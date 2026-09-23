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
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.db import repository
from app.graph.deps import Deps
from app.graph.state import PlanTask, TaskStatus, new_state
from app.graph.workflows.claims.graph import (
    ESCALATION_LIMIT,
    SECOND_REVIEW_LIMIT,
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
    ) -> None:
        self.reply = reply
        self.fields = fields or []
        self.raises = raises
        self.calls: list[str] = []
        self._schema: Any = None

    def with_structured_output(self, schema, **_kw):
        clone = StubModel(self.reply, self.fields, self.raises)
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
            parsed = self._schema(supplied=[{"name": n, "value": v} for n, v in self.fields])
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


def make_state(*, task_args: dict | None = None):
    """State as the supervisor hands it over: one task already RUNNING."""
    state = new_state(session_id="s1", user_id="u1", trace_id="t1")
    state["customer_id"] = "cust-uuid"
    state["plan"] = [
        PlanTask(
            id="1",
            workflow="claims",
            action="retrieve_claims",
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
    seed = Path(__file__).resolve().parents[3] / "data" / "seed" / "seed.sql"
    # The policy text is stored as adjacent SQL string literals wrapped across
    # lines, so a sentence is split by `' \n '`. Join them back, or the assertion
    # only proves the sentence is absent from one particular line-wrapping.
    policy = re.sub(r"'\s*\n\s*'", "", seed.read_text(encoding="utf-8"))

    assert f"exceeds {ESCALATION_LIMIT}" in policy
    assert f"Claims of {SECOND_REVIEW_LIMIT} or more require a second review" in policy
    assert f"Motor claims under {SECOND_REVIEW_LIMIT}" in policy
