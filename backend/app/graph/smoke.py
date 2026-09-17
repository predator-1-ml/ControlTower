"""Day-1 smoke graph: proves durable execution end to end, with no LLM involved.

This is scaffolding for the real graph, but it is not throwaway. It isolates the
single property the whole architecture rests on — *a workflow paused mid-run can
be resumed by a different process* — so that when the real graph misbehaves you
already know whether checkpointing is the cause.

Shape:

    START -> stage_one -> ask_human (interrupt) -> stage_two -> END

Run it, answer the interrupt, and it completes. Run it, kill the process at the
interrupt, start a new one with the same thread_id, and it picks up exactly where
it stopped. Same code path the onboarding workflow's `request_information` node
will use.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict


class SmokeState(TypedDict):
    steps: Annotated[list[str], operator.add]
    answer: str | None


def stage_one(state: SmokeState) -> dict[str, Any]:
    return {"steps": ["stage_one"]}


def ask_human(state: SmokeState) -> dict[str, Any]:
    """Pause and wait for input.

    `interrupt()` is called FIRST, before any other statement in this node, and
    that placement is deliberate rather than stylistic.

    On resume, LangGraph re-runs the node **from the top** — it does not continue
    from the interrupt() line. Anything executed before the interrupt therefore
    runs a second time. Put side effects after the interrupt (or in their own
    node) and this is a non-issue; get it wrong and you get duplicate writes that
    only appear once a human actually pauses the workflow.
    """
    answer = interrupt(
        {
            "kind": "smoke_test",
            "question": "Type anything to prove resume works.",
        }
    )
    return {"steps": ["ask_human"], "answer": str(answer)}


def stage_two(state: SmokeState) -> dict[str, Any]:
    return {"steps": ["stage_two"]}


def build_smoke_graph(checkpointer: BaseCheckpointSaver | None = None):
    """Compile the smoke graph.

    Without a checkpointer `interrupt()` raises — durable execution requires
    both a checkpointer and a `thread_id`. That is a feature: it makes the
    dependency impossible to forget.
    """
    return (
        StateGraph(SmokeState)
        .add_node("stage_one", stage_one)
        .add_node("ask_human", ask_human)
        .add_node("stage_two", stage_two)
        .add_edge(START, "stage_one")
        .add_edge("stage_one", "ask_human")
        .add_edge("ask_human", "stage_two")
        .add_edge("stage_two", END)
        .compile(checkpointer=checkpointer)
    )
