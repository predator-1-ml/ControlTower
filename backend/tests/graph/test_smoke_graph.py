"""Interrupt / resume semantics, against an in-memory checkpointer.

This covers the *logic* of durable execution and pins the LangGraph 1.x API
surface. It deliberately does NOT prove cross-process durability — a real
process death against Postgres is a separate check (`make verify-resume`),
because an in-memory saver cannot fail the way a redeployed container can.
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.graph.smoke import build_smoke_graph


@pytest.fixture
def graph():
    return build_smoke_graph(InMemorySaver())


def config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def test_graph_pauses_at_interrupt(graph):
    result = graph.invoke({"steps": [], "answer": None}, config("t1"))

    assert result["__interrupt__"], "expected the graph to pause"
    assert result["__interrupt__"][0].value["kind"] == "smoke_test"
    # stage_two must NOT have run yet.
    assert result["steps"] == ["stage_one"]


def test_resume_completes_the_run(graph):
    cfg = config("t2")
    graph.invoke({"steps": [], "answer": None}, cfg)

    result = graph.invoke(Command(resume="hello"), cfg)

    assert result["answer"] == "hello"
    assert result["steps"] == ["stage_one", "ask_human", "stage_two"]


def test_interrupted_state_is_recoverable_from_the_checkpoint(graph):
    """A new caller holding only the thread_id can discover the open question.

    This is what the API layer does on reconnect, and what the frontend needs in
    order to re-render a pending question after a page refresh.
    """
    cfg = config("t3")
    graph.invoke({"steps": [], "answer": None}, cfg)

    snapshot = graph.get_state(cfg)

    assert snapshot.next == ("ask_human",), "should be parked at the interrupt"
    assert snapshot.interrupts[0].value["kind"] == "smoke_test"
    assert snapshot.values["steps"] == ["stage_one"]


def test_threads_are_isolated(graph):
    """thread_id is the session boundary; two sessions must not share state."""
    graph.invoke({"steps": [], "answer": None}, config("a"))
    graph.invoke({"steps": [], "answer": None}, config("b"))

    graph.invoke(Command(resume="answer-a"), config("a"))

    assert graph.get_state(config("a")).values["answer"] == "answer-a"
    assert graph.get_state(config("b")).values["answer"] is None


def test_missing_checkpointer_fails_late_not_early():
    """Pins a genuinely nasty failure mode, found while scaffolding.

    A graph compiled without a checkpointer does NOT reject `interrupt()`. It
    pauses, returns a well-formed `__interrupt__`, and looks entirely healthy.
    The failure is deferred to the resume — which in production means a user
    answers a question and *then* the request blows up, with the workflow
    unrecoverable because nothing was ever persisted.

    The lesson encoded here: "the graph paused correctly" is not evidence that
    checkpointing works. Only a resume proves that. Hence `make verify-resume`.
    """
    graph = build_smoke_graph(checkpointer=None)
    cfg = config("nope")

    # Pause looks perfectly fine...
    paused = graph.invoke({"steps": [], "answer": None}, cfg)
    assert paused["__interrupt__"], "pauses happily with no checkpointer"

    # ...and only the resume reveals it.
    with pytest.raises(RuntimeError, match="without checkpointer"):
        graph.invoke(Command(resume="hi"), cfg)

    with pytest.raises(ValueError, match="No checkpointer set"):
        graph.get_state(cfg)
