"""Planner + supervisor + workflows, wired together.

No database, no API key: the pool is stubbed and the planner's model returns a
fixed Plan. What is under test is the orchestration — dependency ordering,
workflow switching, failure propagation, and the recursion ceiling — not the
model's ability to produce a good plan.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import Runtime
from langgraph.types import Command

from app.api.streaming import translate
from app.db import repository
from app.graph.build import RECURSION_LIMIT, build_graph
from app.graph.compose import (
    ONBOARDING_OUTCOMES,
    claims_facts,
    compose_response,
    model_input,
    onboarding_facts,
    written_texts,
)
from app.graph.deps import Deps
from app.graph.state import Plan, PlanTask, TaskStatus, new_state

# --------------------------------------------------------------------- stubs


class StubPool:
    """No methods: audit writes genuinely fail and must be swallowed."""


@dataclass
class StubResponse:
    content: str


class StubModel:
    """Serves the planner a fixed Plan and everything else a fixed string.

    **Structured calls dispatch on the schema.** There are two of them now — the
    planner asks for `Plan`, the claims workflow's `read_reply` asks for `Reply` —
    and a stub that hands a `Plan` to every structured call gives `read_reply` an
    object with no `.supplied`, which fails as an AttributeError a long way from
    the cause. `fields` is what a `Reply` should come back with.

    `calls` counts plain (unstructured) invocations, which is how "compose made
    zero model calls" is asserted.
    """

    def __init__(
        self,
        plan: Plan | None = None,
        reply: str = "Done.",
        fields: list[tuple[str, str]] | None = None,
    ) -> None:
        self.plan = plan
        self.reply = reply
        self.fields = fields or []
        self._schema: Any = None
        self.plan_calls = 0
        self.calls = 0

    def with_structured_output(self, schema, **_kw):
        clone = StubModel(self.plan, self.reply, self.fields)
        clone._schema = schema
        self._child = clone
        return clone

    async def ainvoke(self, _messages, *_a: Any, **_kw: Any):
        if self._schema is Plan:
            self.plan_calls += 1
            return {"parsed": self.plan, "raw": None, "parsing_error": None}
        if self._schema is not None:
            parsed = self._schema(supplied=[{"name": n, "value": v} for n, v in self.fields])
            return {"parsed": parsed, "raw": None, "parsing_error": None}
        self.calls += 1
        return StubResponse(self.reply)


def deps(model: StubModel) -> Deps:
    return Deps(pool=StubPool(), model=model)


def state_with(message: str):
    s = new_state(session_id="s1", user_id="u1", trace_id="t1")
    s["messages"] = [HumanMessage(content=message)]
    return s


def cfg(thread: str) -> dict:
    return {"configurable": {"thread_id": thread}, "recursion_limit": RECURSION_LIMIT}


@pytest.fixture(autouse=True)
def stub_db(monkeypatch):
    """Database responses shared by every test here."""
    customers = {
        "CUST-1002": {
            "id": "id-1002",
            "external_ref": "CUST-1002",
            "full_name": "Daniel Okafor",
            "email": "d@example.com",
            "date_of_birth": "1995-11-03",
            "kyc_status": "verified",
        },
        "CUST-1003": {
            "id": "id-1003",
            "external_ref": "CUST-1003",
            "full_name": "Mei Lin",
            "email": "m@example.com",
            "date_of_birth": "1979-02-27",
            "kyc_status": "failed",
        },
    }

    async def get_customer(_pool, ref):
        return customers.get(ref)

    async def get_active_claims(_pool, _customer_id):
        return []

    async def get_claim(_pool, ref):
        return {
            "claim_ref": ref,
            "customer_name": "Daniel Okafor",
            "customer_ref": "CUST-1002",
            "claim_type": "motor",
            "status": "open",
            "amount": 100.0,
            "incident_date": "2026-01-01",
            # CLM-5003 is the seed's incomplete claim, and it is incomplete here
            # too: a stub that disagrees with the seed makes the branch these
            # tests exercise the one the demo never takes.
            "missing_fields": ["incident_report", "police_reference"]
            if ref == "CLM-5003"
            else [],
        }

    async def create_application(_pool, customer_id, product, status):
        return {"id": "app-1", "customer_id": customer_id, "product": product, "status": status}

    monkeypatch.setattr(repository, "get_customer", get_customer)
    monkeypatch.setattr(repository, "get_active_claims", get_active_claims)
    monkeypatch.setattr(repository, "get_claim", get_claim)
    monkeypatch.setattr(repository, "create_application", create_application)


# --------------------------------------------------------------------- tests


async def test_worked_example_plans_before_executing():
    """The assignment's own example, end to end.

    'Onboard this customer and check whether they already have an active claim'
    must produce a dependency edge and execute in that order.
    """
    plan = Plan(
        goal="Onboard CUST-1002 and check for active claims",
        tasks=[
            PlanTask(id="1", workflow="onboarding", action="onboard_customer",
                     args={"customer_ref": "CUST-1002"}),
            PlanTask(id="2", workflow="claims", action="retrieve_claims",
                     args={"customer_ref": "CUST-1002"}, depends_on=["1"]),
        ],
    )
    graph = build_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        state_with("Onboard CUST-1002 and check if they have an active claim"),
        cfg("worked-example"),
        context=deps(StubModel(plan)),
    )

    by_id = {t.id: t for t in result["plan"]}
    assert by_id["t1"].status is TaskStatus.DONE
    assert by_id["t2"].status is TaskStatus.DONE
    # The dependency survived renumbering — this is the bit that silently breaks.
    assert by_id["t2"].depends_on == ["t1"]
    assert result["final_response"]


async def test_dependency_blocks_until_prerequisite_completes():
    """A task whose dependency never completes must be SKIPPED, not FAILED.

    It did not fail; it never ran. The distinction is what lets the final
    response explain itself.
    """
    plan = Plan(
        goal="unreachable work",
        tasks=[
            PlanTask(id="1", workflow="knowledge", action="answer_question"),
            PlanTask(id="2", workflow="claims", action="retrieve_claims", depends_on=["1"]),
        ],
    )
    # include_knowledge=False makes the knowledge task fail deterministically,
    # which is what this test needs: a failure whose dependents must be skipped.
    graph = build_graph(checkpointer=InMemorySaver(), include_knowledge=False)
    result = await graph.ainvoke(
        state_with("look something up then check claims"),
        cfg("skip-chain"),
        context=deps(StubModel(plan)),
    )

    by_id = {t.id: t for t in result["plan"]}
    assert by_id["t1"].status is TaskStatus.FAILED      # knowledge not implemented
    assert by_id["t2"].status is TaskStatus.SKIPPED     # never attempted
    assert "not implemented" in by_id["t1"].error


async def test_second_turn_extends_the_plan_and_keeps_earlier_tasks():
    """Switching workflow mid-session must not discard prior work.

    This is requirement 11, and it falls out of the planner appending rather than
    replacing — there is no workflow-switch branch anywhere in the code.
    """
    graph = build_graph(checkpointer=InMemorySaver())
    config = cfg("two-turns")

    first = Plan(goal="onboard", tasks=[
        PlanTask(id="1", workflow="onboarding", action="onboard_customer",
                 args={"customer_ref": "CUST-1002"})
    ])
    await graph.ainvoke(state_with("onboard CUST-1002"), config, context=deps(StubModel(first)))

    second = Plan(goal="claims", tasks=[
        PlanTask(id="1", workflow="claims", action="summarise_claim",
                 args={"claim_ref": "CLM-5001"})
    ])
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="now summarise claim CLM-5001")]},
        config,
        context=deps(StubModel(second)),
    )

    ids = [t.id for t in result["plan"]]
    assert ids == ["t1", "t2"], "turn two must append, not overwrite turn one"
    assert all(t.status is TaskStatus.DONE for t in result["plan"])


# ------------------------------------------------------- what compose is given
#
# Guard the INPUT, not the output. Asserting on the model's prose would be flaky
# and would pass the moment the prompt changed; asserting on the curated input is
# deterministic, and the input is where the leak actually was.

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def composable(**slices: dict) -> Any:
    """State with one done task per slice, all in this turn."""
    state = new_state(session_id="s", user_id="u", trace_id="t")
    state["messages"] = [HumanMessage(content="Summarise claim CLM-5003.")]
    state["plan"] = [
        PlanTask(id=f"t{i}", workflow=name, action=f"{name}_action", status=TaskStatus.DONE)
        for i, name in enumerate(slices, start=1)
    ]
    state["turn_task_ids"] = [t.id for t in state["plan"]]
    state["workflow_states"] = dict(slices)
    return state


def test_the_model_is_never_shown_state():
    """The bug this whole node was rewritten for.

    `_summarise` handed the model `str(dict)` of each slice with two keys
    excluded, so UUIDs, raw floats and `[t1] claims.retrieve_claims -> done`
    reached the handler's answer. The model can only leak what it is shown.
    """
    state = composable(
        claims={
            "claims": [
                {
                    "id": "44d65b54-51f7-440a-a34a-f4832916bcde",
                    "claim_ref": "CLM-5003",
                    "claim_type": "property",
                    "status": "under_review",
                    "amount": 12750.0,
                    "incident_date": "2026-09-01",
                    "missing_fields": [],
                    "customer_name": "Tom Baker",
                    "customer_ref": "CUST-1004",
                }
            ],
        },
        onboarding={
            "outcome": "application_created",
            "customer": {"id": "0f2b3c44-0000-4000-8000-000000000001",
                         "full_name": "Priya Raman", "external_ref": "CUST-1001"},
            "application": {"id": "9a1e2f66-0000-4000-8000-000000000002",
                            "customer_id": "0f2b3c44-0000-4000-8000-000000000001",
                            "product": "motor_policy", "status": "submitted"},
        },
    )

    prompt = model_input(state)
    assert not UUID_RE.search(prompt), prompt
    assert "claims_action" not in prompt and "onboarding_action" not in prompt
    assert "[t1]" not in prompt and "[t2]" not in prompt
    # The handler's own vocabulary survives.
    assert "CLM-5003" in prompt and "Tom Baker" in prompt and "CUST-1004" in prompt


def test_amounts_and_dates_are_written_the_way_a_handler_reads_them():
    """`12750.0` and `2026-09-01` are column values, not English.

    The date is built as `f"{d.day} {d:%b %Y}"`; `%-d` would be the obvious way
    and raises ValueError on Windows, where this also has to run.
    """
    facts = claims_facts(
        {"claims": [{"claim_ref": "CLM-5003", "claim_type": "property",
                     "status": "under_review", "amount": 12750.0,
                     "incident_date": "2026-09-01"}]}
    )
    assert "Amount claimed: 12,750.00" in facts
    assert "Incident date: 1 Sep 2026" in facts
    assert "Status: Under review" in facts


def test_claims_facts_tolerate_a_claim_with_no_customer_name():
    """`get_active_claims` does not join the customer table; `get_claim` does.

    Both feed the same slice, so the formatter sees both shapes.
    """
    facts = claims_facts(
        {"claims": [{"claim_ref": "CLM-5001", "claim_type": "motor", "status": "open",
                     "amount": 4820.0, "incident_date": "2026-08-21"}]}
    )
    assert facts[0] == "Claim CLM-5001: motor claim"


def test_the_operators_request_leads_the_model_input():
    state = composable(claims={"claims": []})
    assert model_input(state).startswith('Operator\'s request: "Summarise claim CLM-5003."')


def test_a_failed_task_is_still_reported():
    """A half-succeeded request is a Tuesday. Reporting only the good half lies."""
    state = composable(onboarding={"outcome": "application_created"})
    state["plan"].append(
        PlanTask(id="t9", workflow="knowledge", action="answer_question",
                 status=TaskStatus.FAILED, error="embedder unavailable")
    )
    state["turn_task_ids"].append("t9")

    prompt = model_input(state)
    assert "A knowledge task failed: embedder unavailable" in prompt


def test_only_this_turns_workflows_are_reported():
    """The plan and every workflow slice outlive their turn; the report must not.

    Observed live: after onboarding one customer, a second request produced
    "two onboarding tasks were completed" — the composer was handed the whole
    session. `turn_task_ids` is what scopes it.
    """
    state = composable(onboarding={"outcome": "manual_review", "review_reason": "kyc failed"})
    state["plan"].insert(
        0, PlanTask(id="t0", workflow="claims", action="x", status=TaskStatus.DONE)
    )
    state["workflow_states"]["claims"] = {"summary": "An older turn's summary."}

    assert "older turn" not in (model_input(state) or "")
    assert written_texts(state) == []


async def test_a_turn_with_no_tasks_makes_no_model_call_and_invents_nothing():
    """An empty plan is answered in code, not by the model.

    Observed live: the planner emitted no task for "how to do escalation", and
    the composer — told to use CLM- and CUST- references, with none to use —
    wrote "Nothing was done to escalate the situation for CLM-123456 and
    CUST-789101112". Earlier turns' tasks are still in the plan, as they were
    then; `turn_task_ids` is what says none of them are this turn's.
    """
    state = composable(onboarding={"outcome": "application_created"})
    state["turn_task_ids"] = []
    model = StubModel(reply="Nothing was done for CLM-123456 and CUST-789101112.")

    result = await compose_response(state, Runtime(context=deps(model)))

    assert model.calls == 0
    assert "CLM-123456" not in result["final_response"]
    assert "nothing was done" in result["final_response"]


async def test_a_workflow_that_wrote_the_answer_is_not_paraphrased():
    """Single knowledge task: compose makes ZERO model calls.

    Citations are built from the retrieved chunks, not from the model
    (knowledge/graph.py). A second pass over them can only damage them, and a
    damaged citation silently stops being a source chip in the UI.
    """
    answer = "Motor claims of 5000 or more need a second review [claims-handling-policy.md, Motor]."
    state = composable(knowledge={"outcome": "answered", "answer": answer})
    model = StubModel(reply="a paraphrase nobody asked for")

    result = await compose_response(state, Runtime(context=deps(model)))

    assert result["final_response"] == answer
    assert model.calls == 0


async def test_a_mixed_turn_appends_the_written_text_verbatim():
    """Onboarding produces facts to narrate; knowledge produced its own answer."""
    answer = "Identity checks need a photo ID [onboarding-policy.md, Identity verification]."
    state = composable(
        onboarding={"outcome": "manual_review", "review_reason": "identity verification failed"},
        knowledge={"outcome": "answered", "answer": answer},
    )
    model = StubModel(reply="CUST-1001 went to manual review.")

    result = await compose_response(state, Runtime(context=deps(model)))

    assert model.calls == 1
    assert result["final_response"] == "CUST-1001 went to manual review.\n\n" + answer


async def test_two_workflows_that_both_wrote_text_make_no_model_call():
    """Both texts stand as they are: a claim summary beside a cited answer.

    Tried and reverted: calling the model to consolidate these. Shown the
    policy question in the request, Nova Pro answered it itself — a 4,820
    claim "requires a second review" — and invented next steps above the two
    correct texts.
    """
    answer = "Motor claims of 5000 or more need a second review [claims-handling-policy.md, Motor]."
    state = composable(
        knowledge={"outcome": "answered", "answer": answer},
        claims={"outcome": "summarised", "summary": "Claim CLM-9022 is open.",
                "claims": [{"claim_ref": "CLM-9022"}]},
    )
    model = StubModel(reply="CUST-22914 has no other open claims.")

    result = await compose_response(state, Runtime(context=deps(model)))

    assert model.calls == 0
    assert result["final_response"] == answer + "\n\nClaim CLM-9022 is open."


async def test_onboarding_and_a_claim_become_one_answer():
    """The worked example, as the handler saw it live: a narrated block ending
    in an invented next step, then the claim summary with a different one.

    Onboarding has no text of its own, so the model is called — and given the
    claim's FACTS, next step included, rather than having the summary appended
    under its answer. One answer, every next step given, none invented.
    """
    state = composable(
        onboarding={"outcome": "manual_review",
                    "customer": {"full_name": "Priya Raman", "external_ref": "CUST-1001"},
                    "review_reason": "existing active claim(s): CLM-5001"},
        claims={"outcome": "summarised", "summary": "CLM-5001 is open; assess it.",
                "claims": [{"claim_ref": "CLM-5001", "claim_type": "motor",
                            "status": "open", "amount": 4820.0}],
                "next_actions": {"CLM-5001": ["Proceed with standard assessment."]}},
    )
    prompt = model_input(state)
    assert "Claim CLM-5001: motor claim" in prompt
    assert "Next step: Proceed with standard assessment." in prompt
    assert "Next step: Manual review by the onboarding team" in prompt
    assert "CLM-5001 is open; assess it." not in prompt

    model = StubModel(reply="One answer.")
    result = await compose_response(state, Runtime(context=deps(model)))
    assert model.calls == 1
    assert result["final_response"] == "One answer.", "the summary is not appended"


async def test_a_policy_answer_is_flagged_and_appended_when_something_else_needs_narrating():
    state = composable(
        onboarding={"outcome": "application_created"},
        knowledge={"outcome": "answered", "answer": "Rule [doc, sec]."},
    )
    prompt = model_input(state)
    assert "Knowledge\n- A policy answer follows your text." in prompt
    assert "Rule [doc, sec]." not in prompt

    result = await compose_response(state, Runtime(context=deps(StubModel(reply="Onboarded."))))
    assert result["final_response"] == "Onboarded.\n\nRule [doc, sec]."


def test_every_onboarding_outcome_gives_the_model_a_next_step():
    """The model can only copy a next step it was given.

    Observed live on the worked example: onboarding gave none, and the model
    wrote "Next step: Review the details of claim CLM-5001 to determine the
    next steps for onboarding" — invented, above the claim's real next step.
    """
    for outcome in ONBOARDING_OUTCOMES:
        facts = onboarding_facts({"outcome": outcome})
        assert facts[-1].startswith("Next step: "), outcome
    assert "[onboarding-policy.md, Manual review]" in onboarding_facts(
        {"outcome": "manual_review"}
    )[-1]


# ------------------------------------------------------------- the step trail


async def sse_frames(plan: Plan, message: str, thread: str) -> list[tuple[str, dict]]:
    """Run the real graph and translate it, exactly as `/chat` does."""
    graph = build_graph(checkpointer=InMemorySaver())
    stream = graph.astream(
        state_with(message),
        cfg(thread),
        context=deps(StubModel(plan)),
        stream_mode=["updates", "messages"],
        subgraphs=True,
        version="v2",
    )
    return [
        (frame["event"], json.loads(frame["data"]))
        async for frame in translate(stream, trace_id="t1", known={})
    ]


async def test_the_subgraph_namespace_shape_is_what_the_step_labels_assume():
    """Pins LangGraph's `ns`, which the workflow half of every step label comes from.

    `subgraphs=True` namespaces a subgraph node as ("claims:<uuid>",) and leaves
    a top-level node's tuple empty. That is LangGraph's shape, not ours; if it
    changes, every step label silently becomes unmapped and the trail just goes
    quiet — a failure with no error attached to it.
    """
    graph = build_graph(checkpointer=InMemorySaver())
    plan = Plan(goal="g", tasks=[PlanTask(id="1", workflow="claims", action="retrieve_claims",
                                          args={"customer_ref": "CUST-1002"})])
    seen: dict[str, tuple] = {}
    async for chunk in graph.astream(
        state_with("any claims for CUST-1002?"), cfg("ns-shape"),
        context=deps(StubModel(plan)),
        stream_mode=["updates"], subgraphs=True, version="v2",
    ):
        for node in (chunk.get("data") or {}):
            seen[node] = chunk.get("ns") or ()

    assert seen["planner"] == ()
    assert seen["retrieve"][0].split(":")[0] == "claims"


async def test_mapped_nodes_become_steps_and_unmapped_ones_stay_silent():
    """`retrieve` exists in two subgraphs, which is why the map is keyed by both."""
    plan = Plan(
        goal="claims and policy",
        tasks=[
            PlanTask(id="1", workflow="claims", action="retrieve_claims",
                     args={"claim_ref": "CLM-5001"}),
            PlanTask(id="2", workflow="knowledge", action="answer_question"),
        ],
    )
    frames = await sse_frames(plan, "summarise CLM-5001 and check the policy", "steps")
    labels = [payload["label"] for name, payload in frames if name == "step"]

    assert "looked up the claim" in labels
    assert "searched policy documents" in labels      # knowledge's retrieve, not claims'
    assert "wrote the answer" in labels                # compose, at the top level

    # `embed` is a real node that ran and is deliberately unmapped: a handler has
    # no use for it. And no label may be a LangGraph node name.
    assert "embed" not in labels
    assert not {"retrieve", "generate", "compose", "validate"} & set(labels)


async def test_the_pausing_node_has_no_step_label():
    """Its update fires on the RESUME turn, so a label would appear one turn late.

    The trail would then read "…asked you for information" underneath the answer,
    with nothing above the question it is actually about. The frontend adds
    "needs your answer" from the `interrupt` event instead.
    """
    plan = Plan(goal="claim", tasks=[PlanTask(id="1", workflow="claims",
                                              action="summarise_claim",
                                              args={"claim_ref": "CLM-5003"})])
    frames = await sse_frames(plan, "summarise CLM-5003", "paused-steps")
    labels = [payload["label"] for name, payload in frames if name == "step"]

    assert any(name == "interrupt" for name, _ in frames)
    assert labels == ["looked up the claim", "checked it for missing information"]


async def test_failed_identity_routes_to_manual_review():
    plan = Plan(goal="onboard", tasks=[
        PlanTask(id="1", workflow="onboarding", action="onboard_customer",
                 args={"customer_ref": "CUST-1003"})
    ])
    graph = build_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        state_with("onboard CUST-1003"), cfg("manual-review"), context=deps(StubModel(plan))
    )

    onboarding = result["workflow_states"]["onboarding"]
    assert onboarding["outcome"] == "manual_review"
    assert "identity verification failed" in onboarding["review_reason"]


async def test_a_skipped_documents_request_does_not_verify_identity(monkeypatch):
    """Before this, ANY reply to the documents pause set kyc to verified.

    An empty answer is the operator's explicit skip (api/chat.py): identity stays
    unverified and the application goes to a person, with the reason recorded.
    The shared stub's CUST-1002 is `verified` (it never pauses), so this test
    supplies an unverified one.
    """
    async def get_customer(_pool, ref):
        return {"id": "id-1002", "external_ref": ref, "full_name": "Daniel Okafor",
                "email": "d@example.com", "date_of_birth": "1995-11-03",
                "kyc_status": "unverified"}

    monkeypatch.setattr(repository, "get_customer", get_customer)
    plan = Plan(goal="onboard", tasks=[
        PlanTask(id="1", workflow="onboarding", action="onboard_customer",
                 args={"customer_ref": "CUST-1002"})
    ])
    graph = build_graph(checkpointer=InMemorySaver())
    config = cfg("skip-docs")
    paused = await graph.ainvoke(
        state_with("onboard CUST-1002"), config, context=deps(StubModel(plan))
    )
    assert paused["__interrupt__"][0].value["kind"] == "need_documents"

    result = await graph.ainvoke(Command(resume=""), config, context=deps(StubModel(plan)))

    onboarding = result["workflow_states"]["onboarding"]
    assert onboarding["kyc"] != "verified"
    assert onboarding["outcome"] == "manual_review"
    assert onboarding["review_reason"] == "identity documents not supplied"
    assert result["plan"][0].status is TaskStatus.DONE


async def test_unknown_customer_is_handled_not_crashed():
    plan = Plan(goal="onboard", tasks=[
        PlanTask(id="1", workflow="onboarding", action="onboard_customer",
                 args={"customer_ref": "CUST-9999"})
    ])
    graph = build_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        state_with("onboard CUST-9999"), cfg("unknown"), context=deps(StubModel(plan))
    )

    assert result["workflow_states"]["onboarding"]["outcome"] == "customer_not_found"
    assert result["plan"][0].status is TaskStatus.DONE


async def test_unparseable_plan_is_recorded_not_raised():
    """The planner runs every turn; one bad parse must not kill the session."""
    graph = build_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        state_with("gibberish"), cfg("bad-plan"), context=deps(StubModel(plan=None))
    )

    assert result["errors"][0]["kind"] == "plan_parse_failed"
    assert result["final_response"]  # still answers the user


async def test_large_plan_does_not_hit_the_recursion_ceiling():
    """12 tasks exceeds LangGraph's default recursion_limit of 25.

    2 super-steps per task means the default dies at ~11. This is the exact
    silent cliff a 3-task demo never reaches.
    """
    plan = Plan(
        goal="many claims",
        tasks=[
            # 6000-range on purpose: CLM-5003 is the stub's incomplete claim and
            # would pause the run, which is a different test.
            PlanTask(id=str(i), workflow="claims", action="summarise_claim",
                     args={"claim_ref": f"CLM-{6000 + i}"})
            for i in range(1, 13)
        ],
    )
    graph = build_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        state_with("summarise all the claims"), cfg("big-plan"), context=deps(StubModel(plan))
    )

    assert len(result["plan"]) == 12
    assert all(t.status is TaskStatus.DONE for t in result["plan"])
