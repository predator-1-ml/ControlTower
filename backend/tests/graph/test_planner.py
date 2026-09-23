"""What the planner is shown. Guard the INPUT: the plan itself is the model's.

Built from a live failure. "Summarise CLM-5003" was answered "…for Tom Baker
(CUST-1004)"; the follow-up "register a new claim for 100,000" was then planned
with no customer, because the planner was handed human turns only and the
reference had been said by the assistant.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from app.graph.deps import Deps
from app.graph.planner import plan_node
from app.graph.state import Plan, new_state


class RecordingModel:
    """Returns an empty plan and keeps the messages it was given."""

    def __init__(self) -> None:
        self.messages: list[Any] = []

    def with_structured_output(self, _schema, **_kw):
        return self

    async def ainvoke(self, messages, *_a: Any, **_kw: Any):
        self.messages = messages
        return {"parsed": Plan(goal="", tasks=[]), "raw": None, "parsing_error": None}


def state_after_a_claim_lookup():
    state = new_state(session_id="s", user_id="u", trace_id="t")
    state["customer_ref"] = "CUST-1004"
    state["messages"] = [
        HumanMessage(content="Summarise claim CLM-5003."),
        AIMessage(content="Claim CLM-5003 is a property claim for Tom Baker (CUST-1004)."),
        HumanMessage(content="can we register a new claim CLM-7007 with cost 100,000"),
    ]
    return state


async def test_the_planner_sees_both_sides_and_the_customer_in_focus():
    model = RecordingModel()
    await plan_node(state_after_a_claim_lookup(), Runtime(context=Deps(pool=None, model=model)))

    system, request = (str(m.content) for m in model.messages)
    assert "Customer in focus: CUST-1004" in system
    # The assistant's answer is context now, and it is fenced off from the
    # request: the model plans the last line and only the last line.
    assert "Assistant: Claim CLM-5003 is a property claim for Tom Baker" in request
    assert "Handler: Summarise claim CLM-5003." in request
    assert request.endswith(
        "Plan ONLY this request:\ncan we register a new claim CLM-7007 with cost 100,000"
    )


async def test_a_fresh_session_says_so():
    model = RecordingModel()
    state = new_state(session_id="s", user_id="u", trace_id="t")
    state["messages"] = [HumanMessage(content="onboard CUST-1005")]
    await plan_node(state, Runtime(context=Deps(pool=None, model=model)))

    system, request = (str(m.content) for m in model.messages)
    assert "Customer in focus: none yet" in system
    assert "(none)" in request
