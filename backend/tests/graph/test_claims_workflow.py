"""Claims workflow: every branch, with no database and no API key.

The nodes take their pool and model from `Runtime[Deps]`, so a test supplies a
stub for each. That is the payoff of injecting dependencies rather than importing
a module-level client: every branch is reachable in milliseconds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.db import repository
from app.graph.deps import Deps
from app.graph.state import PlanTask, TaskStatus, new_state
from app.graph.workflows.claims.graph import (
    ESCALATION_LIMIT,
    REQUIRED_DOCUMENTS,
    SECOND_REVIEW_LIMIT,
    ClaimDetails,
    build_claims_graph,
    next_actions,
)

# --------------------------------------------------------------------- stubs


class StubPool:
    """Stands in for the psycopg pool.

    Deliberately has no methods. Every repository call is patched except
    `record_audit_event`, which therefore genuinely fails against this stub,
    proving audit failures cannot break a workflow.
    """


@dataclass
class StubResponse:
    content: str


class StubModel:
    """Chat-model stand-in that dispatches structured calls on their schema.

    This workflow now makes two kinds of call: a plain one (`summarise`) and a
    structured one (`read_reply`, which asks for `Reply`). A stub that answered
    every structured call the same way would hand `read_reply` an object with no
    `.supplied` — an AttributeError a long way from its cause.

    `fields` is what the model claims the operator supplied. A test can make it
    lie (a value that is not in the reply) to prove the verification step works.
    """

    def __init__(
        self,
        reply: str = "Claim summary.",
        fields: list[tuple[str, str]] | None = None,
        raises: Exception | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.reply = reply
        self.fields = fields or []
        self.raises = raises
        # What the model claims a registration reply contained (`ClaimDetails`).
        self.details = details or {}
        self.calls: list[str] = []
        self._schema: Any = None

    def with_structured_output(self, schema, **_kw):
        clone = StubModel(self.reply, self.fields, self.raises, self.details)
        clone._schema = schema
        clone.calls = self.calls  # one shared record of what the model was asked
        return clone

    async def ainvoke(self, prompt: Any, *_: Any, **__: Any) -> Any:
        self.calls.append(str(prompt))
        if self._schema is not None:
            # `raises` applies to the structured call only: the failure under
            # test is the extraction one, and a stub that also broke `summarise`
            # would prove the workflow survives a different failure than the one
            # named in the test.
            if self.raises is not None:
                raise self.raises
            if self._schema is ClaimDetails:
                parsed = ClaimDetails(**self.details)
            else:
                parsed = self._schema(
                    supplied=[{"name": n, "value": v} for n, v in self.fields]
                )
            return {"parsed": parsed, "raw": None, "parsing_error": None}
        return StubResponse(self.reply)


CLAIM_COMPLETE = {
    "claim_ref": "CLM-5001",
    "customer_name": "Priya Raman",
    "customer_ref": "CUST-1001",
    "claim_type": "motor",
    "status": "open",
    "amount": 4820.0,
    "incident_date": "2026-08-21",
    "missing_fields": [],
}

CLAIM_INCOMPLETE = {
    **CLAIM_COMPLETE,
    "claim_ref": "CLM-5003",
    "customer_name": "Tom Baker",
    "customer_ref": "CUST-1004",
    "claim_type": "property",
    "status": "awaiting_information",
    "amount": 12750.0,
    "incident_date": "2026-09-01",
    "missing_fields": ["incident_report", "police_reference"],
}


@pytest.fixture
def graph():
    return build_claims_graph()


@pytest.fixture
def db_returns(monkeypatch):
    """Control what the retrieve node finds, and capture the write.

    Patching the repository is what these tests actually need. Pre-seeding
    `workflow_states` does not work: `retrieve` always runs and overwrites its
    own slice, so the fixture data is silently discarded and every branch falls
    through to not_found. That mistake cost a full test run, which is why this is
    a fixture rather than a comment.

    `StubPool` has no methods, so `record_claim_information` must be patched too
    or the write raises — and unlike the audit write, this one is not swallowed.
    """
    written: list[tuple[str, list[str]]] = []

    def _set(claims: list[dict]) -> list[tuple[str, list[str]]]:
        async def fake_active(_pool, _customer_id):
            return claims

        async def fake_get(_pool, _claim_ref):
            return claims[0] if claims else None

        async def fake_record(_pool, claim_ref, received):
            written.append((claim_ref, received))
            remaining = [f for f in claims[0].get("missing_fields", []) if f not in received]
            return {
                "status": "under_review" if not remaining else "awaiting_information",
                "missing_fields": remaining,
            }

        monkeypatch.setattr(repository, "get_active_claims", fake_active)
        monkeypatch.setattr(repository, "get_claim", fake_get)
        monkeypatch.setattr(repository, "record_claim_information", fake_record)
        return written

    return _set


def make_state(
    *, task_args: dict | None = None, action: str = "retrieve_claims", request: str = ""
):
    """State as the supervisor hands it over: one task already RUNNING.

    `request` is what the handler typed. The register path keeps a reference or
    a type from the planner's args only if it occurs there (`_stated`), so a
    test that supplies either must also supply the words.
    """
    state = new_state(session_id="s1", user_id="u1", trace_id="t1")
    state["customer_id"] = "cust-uuid"
    state["customer_ref"] = "CUST-1004"
    state["messages"] = [HumanMessage(content=request)]
    state["plan"] = [
        PlanTask(
            id="1",
            workflow="claims",
            action=action,
            args=task_args or {},
            status=TaskStatus.RUNNING,
        )
    ]
    return state


def deps(model: StubModel | None = None) -> Deps:
    return Deps(pool=StubPool(), model=model or StubModel())


def resumable(db_returns, claims: list[dict]):
    """A checkpointed graph parked on the pause, plus its config and writes log.

    Compiled with its own saver because there is no parent graph here to inherit
    one from.
    """
    written = db_returns(claims)
    graph = build_claims_graph(checkpointer=InMemorySaver())
    return graph, {"configurable": {"thread_id": f"claims-{id(claims)}"}}, written


# --------------------------------------------------------------------- tests


async def test_complete_claim_is_summarised(graph, db_returns):
    db_returns([CLAIM_COMPLETE])
    model = StubModel("Motor claim CLM-5001 is open pending assessment.")
    result = await graph.ainvoke(make_state(), context=deps(model))

    claims_state = result["workflow_states"]["claims"]
    assert claims_state["outcome"] == "summarised"
    assert claims_state["summary"] == "Motor claim CLM-5001 is open pending assessment."
    assert result["plan"][0].status is TaskStatus.DONE


async def test_the_summary_prompt_carries_facts_and_the_decided_next_step(graph, db_returns):
    """Guard the INPUT: asserting on the model's prose would be flaky.

    The summary must be written from formatted facts and from the next step
    `assess` already chose — never from raw state, and never from a decision the
    model made itself.
    """
    # CLM-5003 as it stands once the pause has been satisfied: the demo's own
    # claim, and the one over the escalation threshold.
    db_returns([{**CLAIM_INCOMPLETE, "id": "44d65b54-51f7-440a-a34a-f4832916bcde",
                 "status": "under_review", "missing_fields": []}])
    model = StubModel()
    await graph.ainvoke(make_state(), context=deps(model))

    prompt = model.calls[0]
    assert "CLM-5003" in prompt and "Tom Baker" in prompt and "CUST-1004" in prompt
    assert "12,750.00" in prompt and "1 Sep 2026" in prompt
    assert "Escalate to the duty manager: the claim exceeds 10,000" in prompt
    assert "[operations-runbook.md, Escalation]" in prompt
    # Neither the raw float nor the row id may reach the model.
    assert "12750.0" not in prompt
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", prompt)


async def test_incomplete_claim_pauses_instead_of_summarising(graph, db_returns):
    db_returns([CLAIM_INCOMPLETE])
    model = StubModel()
    result = await graph.ainvoke(make_state(), context=deps(model))

    interrupts = result["__interrupt__"]
    assert interrupts, "expected a pause for missing information"
    payload = interrupts[0].value
    assert payload["kind"] == "need_info"
    assert payload["fields"] == ["incident_report", "police_reference"]

    # Deciding to pause is a business rule, so the model must not be consulted at
    # all. A fluent summary of an incomplete claim is exactly the confident-but-
    # wrong output this split exists to prevent.
    assert model.calls == []


async def test_the_pause_asks_about_one_claim_only(graph, db_returns):
    """Two incomplete claims must not produce one question about both.

    `validate` used to union every incomplete claim's missing fields, so a reply
    supplying "police reference" had no owning claim to record it against. No
    seeded customer has two incomplete claims, so nothing in the demo — and no
    other test — would ever surface this.
    """
    second = {**CLAIM_INCOMPLETE, "claim_ref": "CLM-5004", "missing_fields": ["repair_estimate"]}
    db_returns([CLAIM_INCOMPLETE, second])

    result = await graph.ainvoke(make_state(), context=deps())
    payload = result["__interrupt__"][0].value

    assert payload["fields"] == ["incident_report", "police_reference"]
    assert "CLM-5003" in payload["question"] and "CLM-5004" not in payload["question"]
    assert "repair estimate" not in payload["question"]


async def test_no_claims_is_a_successful_outcome_not_a_failure(graph, db_returns):
    """'No active claims' correctly answers the assignment's worked example."""
    db_returns([])
    result = await graph.ainvoke(make_state(), context=deps())

    assert result["workflow_states"]["claims"]["outcome"] == "no_claims"
    assert result["plan"][0].status is TaskStatus.DONE
    assert result["errors"] == []


async def test_a_complete_reply_updates_the_claim_and_summarises_it(db_returns):
    """The whole point of the pause: the reply changes the claim row."""
    graph, config, written = resumable(db_returns, [dict(CLAIM_INCOMPLETE)])
    model = StubModel(
        fields=[("incident_report", "IR-2291"), ("police_reference", "PR-77431")]
    )
    await graph.ainvoke(make_state(), config, context=deps(model))

    # Text, exactly as `/chat` resumes — never a dict.
    result = await graph.ainvoke(
        Command(resume="Incident report IR-2291, police ref PR-77431"),
        config,
        context=deps(model),
    )

    claims_state = result["workflow_states"]["claims"]
    assert written == [("CLM-5003", ["incident_report", "police_reference"])]
    assert claims_state["received"]["incident_report"] == "IR-2291"
    assert claims_state["claims"][0]["status"] == "under_review"
    assert claims_state["claims"][0]["missing_fields"] == []
    assert claims_state["outcome"] == "summarised"
    assert result["plan"][0].status is TaskStatus.DONE


async def test_a_partial_reply_asks_again_for_only_what_is_left(db_returns):
    """Progress, not a counter, is what bounds the loop."""
    graph, config, written = resumable(db_returns, [dict(CLAIM_INCOMPLETE)])
    model = StubModel(fields=[("police_reference", "PR-77431")])
    first = await graph.ainvoke(make_state(), config, context=deps(model))

    second = await graph.ainvoke(
        Command(resume="the police reference is PR-77431, still chasing the report"),
        config,
        context=deps(model),
    )

    asked = second["__interrupt__"][0]
    assert asked.value["fields"] == ["incident_report"]
    assert "police" not in asked.value["question"]
    # A new id, so the stream's interrupt dedupe cannot swallow the second ask.
    assert asked.id != first["__interrupt__"][0].id
    assert written == [], "nothing may be written while the pause is still open"


async def test_a_reply_that_supplies_nothing_ends_the_pause_honestly(db_returns):
    """A new request typed at the pause is consumed as an answer (tradeoffs.md).

    It extracts nothing, so the pause ends rather than looping — and the claim is
    reported as still waiting, not silently ignored.
    """
    graph, config, written = resumable(db_returns, [dict(CLAIM_INCOMPLETE)])
    model = StubModel(fields=[])
    await graph.ainvoke(make_state(), config, context=deps(model))

    result = await graph.ainvoke(
        Command(resume="what does this claim need again?"), config, context=deps(model)
    )

    claims_state = result["workflow_states"]["claims"]
    assert "__interrupt__" not in result, "a useless reply must not re-ask"
    assert written == []
    assert claims_state["outcome"] == "information_incomplete"
    assert claims_state["claims"][0]["missing_fields"] == ["incident_report", "police_reference"]
    assert result["plan"][0].status is TaskStatus.DONE


async def test_an_explicit_skip_ends_the_pause_without_asking_the_model(db_returns):
    """An empty answer is the operator's "I don't have this yet" (api/chat.py).

    The model is NOT asked to read it: the Nova Pro probe showed it invents
    references when the reply contains none, and the verification guard would
    still accept a value like "this" if the reply happened to contain it.
    """
    graph, config, written = resumable(db_returns, [dict(CLAIM_INCOMPLETE)])
    model = StubModel(fields=[("incident_report", "IR-1")])  # would lie, if asked
    await graph.ainvoke(make_state(), config, context=deps(model))

    result = await graph.ainvoke(Command(resume=""), config, context=deps(model))

    claims_state = result["workflow_states"]["claims"]
    assert "__interrupt__" not in result
    assert not any("Reply" in c or "supplied" in c for c in model.calls), "no extraction call"
    assert written == []
    assert claims_state["outcome"] == "information_incomplete"
    assert result["plan"][0].status is TaskStatus.DONE


async def test_an_invented_value_is_discarded(db_returns):
    """The verification step, with a lying stub.

    The model returns a reference the operator never wrote. Requiring the value
    to occur in their own words is what makes invention impossible — it does not
    make the value *correct*, only genuinely theirs.
    """
    graph, config, written = resumable(db_returns, [dict(CLAIM_INCOMPLETE)])
    model = StubModel(
        fields=[("incident_report", "IR-0001"), ("police_reference", "PR-77431")]
    )
    await graph.ainvoke(make_state(), config, context=deps(model))

    result = await graph.ainvoke(
        Command(resume="police ref PR-77431, no incident report yet"),
        config,
        context=deps(model),
    )

    received = result["workflow_states"]["claims"]["received"]
    assert received == {"police_reference": "PR-77431"}
    # The invented reference did not narrow anything, so the workflow asks again
    # for exactly the field it never really got — and writes nothing meanwhile.
    assert result["__interrupt__"][0].value["fields"] == ["incident_report"]
    assert written == []


async def test_a_model_failure_in_read_reply_is_recorded_not_raised(db_returns):
    """Uncaught, this leaves the task RUNNING with the interrupt already consumed.

    The operator's next message is then planned as a new request and they are
    asked the same question again, from the top, with their answer gone.
    `include_raw=True` does not help here: a transport error raises.
    """
    graph, config, written = resumable(db_returns, [dict(CLAIM_INCOMPLETE)])
    model = StubModel(raises=RuntimeError("bedrock said no"))
    await graph.ainvoke(make_state(), config, context=deps(StubModel()))

    result = await graph.ainvoke(
        Command(resume="Incident report IR-2291"), config, context=deps(model)
    )

    assert result["errors"][0]["kind"] == "reply_unreadable"
    assert result["workflow_states"]["claims"]["reply_unreadable"] is True
    assert written == []
    assert result["plan"][0].status is TaskStatus.DONE, "must not be left RUNNING"


async def test_task_update_is_a_delta_not_the_whole_plan(graph, db_returns):
    """Guards the merge_tasks contract where it is easiest to break.

    A node returning the full plan reverts concurrent updates. `task_delta`
    exists to make that impossible; this asserts the workflow actually uses it.
    """
    db_returns([CLAIM_COMPLETE])
    state = make_state()
    state["plan"].append(
        PlanTask(id="2", workflow="onboarding", action="verify", status=TaskStatus.RUNNING)
    )

    result = await graph.ainvoke(state, context=deps())

    # Task 2 belongs to another workflow and must be untouched by this one.
    by_id = {t.id: t for t in result["plan"]}
    assert by_id["1"].status is TaskStatus.DONE
    assert by_id["2"].status is TaskStatus.RUNNING


async def test_audit_failure_does_not_break_the_workflow(graph, db_returns):
    """StubPool has no connection method, so every audit write raises internally.

    The workflow must still complete: losing an audit row is not a reason to fail
    the operation that produced it.
    """
    db_returns([CLAIM_COMPLETE])
    result = await graph.ainvoke(make_state(), context=deps())
    assert result["workflow_states"]["claims"]["outcome"] == "summarised"


# ------------------------------------------------------------ handling rules


def test_next_actions_covers_every_rule():
    """All five rows of the table in the module docstring.

    Two rules can apply at once, which is why this returns a list rather than a
    string — a motor claim over the escalation limit needs both.
    """
    assert next_actions({"status": "settled"}) == ["None: the claim is closed."]
    assert next_actions({"status": "open", "amount": 100}) == [
        "Proceed with standard assessment by a single assessor."
    ]

    missing = next_actions({"status": "awaiting_information", "amount": 100,
                            "missing_fields": ["police_reference"]})
    assert missing[0].startswith("Still needed: police reference")
    assert "[claims-handling-policy.md, Missing information]" in missing[0]

    escalated = next_actions({"status": "under_review", "claim_type": "property",
                              "amount": 12750.0})
    assert escalated == [
        "Escalate to the duty manager: the claim exceeds 10,000 "
        "[operations-runbook.md, Escalation]"
    ]

    both = next_actions({"status": "open", "claim_type": "motor", "amount": 12000.0})
    assert len(both) == 2
    assert "duty manager" in both[0] and "second review" in both[1]

    # Restricted to motor: the policy sentence sits under the Motor claims
    # heading, so a 6,000 travel claim must NOT be sent for a second review.
    assert next_actions({"status": "open", "claim_type": "travel", "amount": 6000.0}) == [
        "Proceed with standard assessment by a single assessor."
    ]


def test_the_thresholds_in_code_are_the_thresholds_in_the_policy_text():
    """Makes `docs/assumptions.md`'s "RAG and code agree" checkable, not merely claimed.

    The knowledge workflow answers "when does a motor claim need a second
    review?" from the seeded policy text, and `next_actions` decides it in code.
    If someone tunes one number, these are two sources of truth for the same rule
    and a handler gets two different answers in the same session.
    """
    seed = Path(__file__).resolve().parents[2] / "seed" / "seed.sql"
    # The policy text is stored as adjacent SQL string literals wrapped across
    # lines, so a sentence is split by `' \n '`. Join them back, or the assertion
    # only proves the sentence is absent from one particular line-wrapping.
    policy = re.sub(r"'\s*\n\s*'", "", seed.read_text(encoding="utf-8"))

    assert f"exceeds {ESCALATION_LIMIT}" in policy
    assert f"Claims of {SECOND_REVIEW_LIMIT} or more require a second review" in policy
    assert f"Motor claims under {SECOND_REVIEW_LIMIT}" in policy

    # The same check for what a new claim starts out waiting for.
    assert (
        "Motor and property claims are registered as awaiting information until an "
        "incident report reference is recorded; travel claims open immediately."
    ) in policy
    needs_report = [t for t, docs in REQUIRED_DOCUMENTS.items() if "incident_report" in docs]
    assert needs_report == ["motor", "property"]
    assert REQUIRED_DOCUMENTS["travel"] == []


# ------------------------------------------------------------ register_claim


@pytest.fixture
def registry(monkeypatch):
    """Control the customer lookup and capture the INSERT.

    `create_claim` returns the row as the database would: the reference minted
    when none was given, the status derived from the documents outstanding, and
    the owner joined in. `existing` is what `get_claim` finds for a reference the
    handler supplied.
    """
    created: list[dict] = []

    def _set(customer: dict | None, existing: dict | None = None) -> list[dict]:
        async def fake_customer(_pool, ref):
            return customer if customer and customer["external_ref"] == ref else None

        async def fake_get(_pool, _claim_ref):
            return existing

        async def fake_create(_pool, **kw):
            created.append(kw)
            return {
                "id": "new-uuid",
                "claim_ref": kw["claim_ref"] or "CLM-9001",
                "claim_type": kw["claim_type"],
                "status": "awaiting_information" if kw["missing_fields"] else "open",
                "amount": kw["amount"],
                "incident_date": kw["incident_date"],
                "missing_fields": kw["missing_fields"],
                "customer_id": kw["customer_id"],
                "customer_ref": "CUST-1004",
                "customer_name": "Tom Baker",
            }

        monkeypatch.setattr(repository, "get_customer", fake_customer)
        monkeypatch.setattr(repository, "get_claim", fake_get)
        monkeypatch.setattr(repository, "create_claim", fake_create)
        return created

    return _set


TOM = {"id": "tom-uuid", "external_ref": "CUST-1004", "full_name": "Tom Baker"}


async def test_a_fully_stated_claim_is_registered_without_asking(graph, registry):
    """The handler said everything: no pause, no extraction call, one INSERT."""
    created = registry(TOM)
    model = StubModel()
    result = await graph.ainvoke(
        make_state(
            action="register_claim",
            task_args={"customer_ref": "CUST-1004", "claim_ref": "clm-7007",
                       "claim_type": "Property", "amount": "100,000",
                       "incident_date": "2026-09-20"},
            request="register clm-7007 for CUST-1004: property, 100,000, incident 2026-09-20",
        ),
        context=deps(model),
    )

    assert "__interrupt__" not in result
    assert created == [{
        "customer_id": "tom-uuid",
        "claim_ref": "CLM-7007",
        "claim_type": "property",
        "amount": 100000.0,      # "100,000" read as a number, not rejected
        "incident_date": "2026-09-20",
        "missing_fields": ["incident_report"],
    }]
    ws = result["workflow_states"]["claims"]
    assert ws["outcome"] == "registered"
    assert result["plan"][0].status is TaskStatus.DONE
    # The summary is written from the same facts as a looked-up claim, with the
    # rule the amount triggers already decided in code.
    prompt = model.calls[0]
    assert "Registered this turn" in prompt and "CLM-7007" in prompt
    assert "Escalate to the duty manager: the claim exceeds 10,000" in prompt
    assert "Still missing: incident report" in prompt
    assert not any("ClaimDetails" in c or "extract" in c for c in model.calls)


async def test_register_falls_back_to_the_customer_in_focus(graph, registry):
    """"Register a claim for them": no customer in args, one in shared state."""
    created = registry(TOM)
    await graph.ainvoke(
        make_state(action="register_claim",
                   task_args={"claim_type": "travel", "amount": 300,
                              "incident_date": "2026-09-01"},
                   request="log a travel claim for them, 300, incident 2026-09-01"),
        context=deps(),
    )
    assert created[0]["customer_id"] == "cust-uuid"   # from make_state, not args
    assert created[0]["missing_fields"] == []          # travel opens immediately


async def test_register_with_no_customer_writes_nothing(graph, registry):
    """A refusal with a reason, not a claim registered against nobody."""
    created = registry(customer=None)
    state = make_state(action="register_claim", task_args={"claim_type": "motor"})
    state["customer_id"] = None
    state["customer_ref"] = None

    result = await graph.ainvoke(state, context=deps())

    ws = result["workflow_states"]["claims"]
    assert created == []
    assert ws["outcome"] == "not_registered"
    assert "CUST-" in ws["reason"]
    # Written by the workflow, so compose passes it through with no model call.
    assert ws["summary"].startswith("The claim was not registered: no customer")
    assert result["plan"][0].status is TaskStatus.DONE


async def test_register_refuses_a_reference_that_is_already_taken(graph, registry):
    created = registry(TOM, existing=CLAIM_COMPLETE)
    result = await graph.ainvoke(
        make_state(action="register_claim",
                   task_args={"claim_ref": "CLM-5001", "claim_type": "motor"},
                   request="register a motor claim CLM-5001 for them"),
        context=deps(),
    )
    ws = result["workflow_states"]["claims"]
    assert created == []
    assert ws["outcome"] == "not_registered"
    assert "CLM-5001" in ws["reason"] and "Priya Raman" in ws["reason"]


async def test_register_asks_for_what_is_missing_then_creates(registry):
    """The live transcript this was built from: "CLM-7007 with cost 100,000".

    The type was never stated, so the workflow asks — and asks only for what it
    lacks. The reply is read into typed fields ("20 September" becomes a date),
    and the claim is created with the type the handler wrote.
    """
    created = registry(TOM)
    graph = build_claims_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "register-ask"}}
    model = StubModel(details={"claim_type": "property", "incident_date": "2026-09-20"})

    first = await graph.ainvoke(
        make_state(action="register_claim",
                   task_args={"claim_ref": "CLM-7007", "amount": 100000},
                   request="can we onboard new claim with number CLM-7007 with cost 100,000"),
        config, context=deps(model),
    )
    asked = first["__interrupt__"][0].value
    assert asked["kind"] == "need_info"
    assert asked["fields"] == ["claim_type", "incident_date"]
    assert "CLM-7007" in asked["question"] and "CUST-1004" in asked["question"]
    assert created == [], "nothing may be written while the pause is open"

    result = await graph.ainvoke(
        Command(resume="It's a property claim, the incident was on 20 September 2026"),
        config, context=deps(model),
    )

    assert created[0]["claim_type"] == "property"
    assert created[0]["incident_date"] == "2026-09-20"   # "20 September" passed the guard
    assert created[0]["amount"] == 100000.0
    assert result["workflow_states"]["claims"]["outcome"] == "registered"
    assert result["plan"][0].status is TaskStatus.DONE


async def test_a_type_the_handler_did_not_write_is_discarded(registry):
    """Same guard as the lookup pause: the model may not invent the claim type.

    A wrongly typed claim is a wrong row in the system of record, so a type that
    does not occur in the reply is dropped and the claim is honestly not
    registered — rather than registered as whatever the model guessed.
    """
    created = registry(TOM)
    graph = build_claims_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "register-invent"}}
    model = StubModel(details={"claim_type": "motor"})   # lies

    await graph.ainvoke(
        make_state(action="register_claim", task_args={"amount": 500}),
        config, context=deps(model),
    )
    result = await graph.ainvoke(
        Command(resume="not sure of the type yet, amount is right"),
        config, context=deps(model),
    )

    ws = result["workflow_states"]["claims"]
    assert created == []
    assert ws["outcome"] == "not_registered"
    assert "claim type" in ws["reason"]
    assert "__interrupt__" not in result, "one ask, then decide"


async def test_an_unknown_action_is_a_lookup(graph, db_returns):
    """The planner improvises action names ("summarise_claim"). Only the exact
    register action may reach the write path; everything else reads."""
    db_returns([CLAIM_COMPLETE])
    result = await graph.ainvoke(
        make_state(action="summarise_claim", task_args={"claim_ref": "CLM-5001"}),
        context=deps(),
    )
    assert result["workflow_states"]["claims"]["outcome"] == "summarised"


async def test_retrieve_publishes_the_customer_in_focus(graph, db_returns):
    """Only onboarding used to publish `customer_id`; a session that opened with
    a claim lookup had no customer for "register one for them" to resolve."""
    db_returns([{**CLAIM_COMPLETE, "customer_id": "priya-uuid"}])
    state = make_state(task_args={"claim_ref": "CLM-5001"})
    state["customer_id"] = None
    state["customer_ref"] = None

    result = await graph.ainvoke(state, context=deps())

    assert result["customer_id"] == "priya-uuid"
    assert result["customer_ref"] == "CUST-1001"


async def test_a_reference_the_handler_did_not_type_is_minted_instead(graph, registry):
    """Observed live: "log a travel claim for them" planned with `claim_ref:
    CLM-5001` — the prompt's own example. A reference absent from the request
    is dropped, and the database mints one, rather than the register being
    refused because the example is someone's claim."""
    created = registry(TOM, existing=CLAIM_COMPLETE)   # CLM-5001 IS taken
    result = await graph.ainvoke(
        make_state(action="register_claim",
                   task_args={"claim_ref": "CLM-5001", "claim_type": "travel",
                              "amount": 450, "incident_date": "2026-09-12"},
                   request="log a travel claim for them for 450, incident on 12 Sep 2026"),
        context=deps(),
    )
    assert created[0]["claim_ref"] is None
    assert result["workflow_states"]["claims"]["outcome"] == "registered"


async def test_a_type_the_handler_did_not_type_is_asked_for(graph, registry):
    """Observed live: after summarising a property claim, a NEW claim was
    planned as `property` — taken from the transcript, not the request."""
    registry(TOM)
    result = await graph.ainvoke(
        make_state(action="register_claim",
                   task_args={"claim_ref": "CLM-7007", "claim_type": "property",
                              "amount": 100000},
                   request="register CLM-7007 for them, 100,000"),
        context=deps(),
    )
    assert result["__interrupt__"][0].value["fields"] == ["claim_type", "incident_date"]


async def test_a_customer_named_by_name_is_looked_up(graph, db_returns, monkeypatch):
    """"does Hiro Tanaka have open claims?" — the planner sends `customer_name`."""
    db_returns([{**CLAIM_COMPLETE, "customer_id": "hiro-uuid", "customer_ref": "CUST-1008"}])
    hiro = {"id": "hiro-uuid", "external_ref": "CUST-1008", "full_name": "Hiro Tanaka"}

    async def fake_find(_pool, name):
        return [hiro] if name.lower() in "hiro tanaka" else []

    monkeypatch.setattr(repository, "find_customers", fake_find)
    state = make_state(task_args={"customer_name": "Tanaka"})
    state["customer_id"] = None

    result = await graph.ainvoke(state, context=deps())

    assert result["workflow_states"]["claims"]["outcome"] == "summarised"
    assert result["customer_ref"] == "CUST-1008"


async def test_an_unknown_or_ambiguous_customer_is_not_a_customer_with_no_claims(
    graph, db_returns, monkeypatch
):
    """"No active claims were found" used to be the answer for a customer who
    does not exist. Now the answer says so, verbatim from the workflow."""
    db_returns([])
    two = [{"id": "a", "external_ref": "CUST-1010", "full_name": "Ben Carter"},
           {"id": "b", "external_ref": "CUST-1099", "full_name": "Ben Adeyemi"}]

    async def fake_find(_pool, name):
        return two if name == "Ben" else []

    monkeypatch.setattr(repository, "find_customers", fake_find)

    for name, expect in (("Ben", "more than one customer matches"),
                         ("Nobody", "no customer called")):
        state = make_state(task_args={"customer_name": name})
        state["customer_id"] = None
        result = await graph.ainvoke(state, context=deps())
        ws = result["workflow_states"]["claims"]
        assert ws["outcome"] == "not_found"
        assert expect in ws["summary"], ws["summary"]
        assert result["plan"][0].status is TaskStatus.DONE


async def test_a_date_or_amount_the_handler_did_not_type_is_asked_for(graph, registry):
    """Observed live: a new claim dated 1 Sep 2026 — the previous claim's date,
    copied from the transcript. Digits and days are checked against the text."""
    registry(TOM)
    result = await graph.ainvoke(
        make_state(action="register_claim",
                   task_args={"claim_type": "motor", "amount": 12750,
                              "incident_date": "2026-09-01"},
                   request="register a motor claim for them"),
        context=deps(),
    )
    assert result["__interrupt__"][0].value["fields"] == ["amount", "incident_date"]


def test_stated_matches_the_ways_a_handler_writes_a_date_and_a_number():
    from app.graph.workflows.claims.graph import _stated

    for text in ("incident on 12 Sep 2026", "on 2026-09-12", "12/09/2026", "the 12th of September"):
        assert _stated("incident_date", "2026-09-12", text), text
    assert not _stated("incident_date", "2026-09-12", "yesterday")
    assert not _stated("incident_date", "2026-09-01", "summarise CLM-5003, 100,000")

    assert _stated("amount", 100000.0, "with cost 100,000")
    assert _stated("amount", 1200.5, "$1,200.50 damage")
    assert not _stated("amount", 12750.0, "register a claim for them")
    assert _stated("claim_ref", "CLM-7007", "number clm-7007 please")
    assert not _stated("claim_type", "property", "a new claim")


def test_details_keep_what_validates_and_drop_the_rest():
    """One bad value must not discard the good ones next to it."""
    from app.graph.workflows.claims.graph import _details

    details = _details({"claim_type": "car", "amount": "$1,200.50", "incident_date": "soon"})
    assert details == ClaimDetails(amount=1200.5)
