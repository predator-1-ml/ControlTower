"""Custom types survive a real Postgres checkpoint round-trip as themselves.

`PlanTask` and `TaskStatus` live inside checkpointed state. LangGraph serialises
state with msgpack and warns about types it was not told to expect; once that
becomes an error it does NOT raise, it hands back the raw dict. A node would then
get a plain dict where it expects a PlanTask, and fail with an AttributeError far
from the cause, or take a wrong branch silently.

`ALLOWED_MSGPACK_MODULES` registers them. This asserts the registration works
against a real database.

Two things were learned writing this test, both worth keeping:

* The isinstance assertions alone prove nothing *today*. Current LangGraph still
  reconstructs unregistered types and only warns, so they pass either way. They
  are here for the version where it stops.
* The warning is emitted through `logging`, not `warnings`, so `recwarn` never
  sees it and an assertion built on it is dead code. `caplog` is the one that
  catches it — and that assertion does fail without the allowlist, verified by
  removing it.
"""

from __future__ import annotations

import logging
import os

import pytest
from langgraph.graph import END, START, StateGraph

from app.db.checkpointer import build_checkpointer, open_pool
from app.graph.state import ControlTowerState, PlanTask, TaskStatus, new_state

pytestmark = pytest.mark.skipif(
    os.environ.get("CONTROL_TOWER_DB_TESTS") != "1",
    reason="needs Postgres; set CONTROL_TOWER_DB_TESTS=1",
)


async def _passthrough(state: ControlTowerState) -> dict:
    return {}


async def test_plan_task_round_trips_as_a_model_not_a_dict(caplog):
    caplog.set_level(logging.WARNING, logger="langgraph.checkpoint.serde.jsonplus")
    pool = await open_pool()
    try:
        graph = (
            StateGraph(ControlTowerState)
            .add_node("noop", _passthrough)
            .add_edge(START, "noop")
            .add_edge("noop", END)
            .compile(checkpointer=build_checkpointer(pool))
        )

        state = new_state(session_id="serde-1", user_id="u", trace_id="t")
        state["plan"] = [
            PlanTask(
                id="1",
                workflow="claims",
                action="retrieve_claims",
                status=TaskStatus.RUNNING,
            )
        ]

        config = {"configurable": {"thread_id": "serde-round-trip"}}
        await graph.ainvoke(state, config)

        # Read back through the checkpointer, which is the deserialisation path
        # that would hand back a dict if the types were unregistered.
        snapshot = await graph.aget_state(config)
        task = snapshot.values["plan"][0]

        assert isinstance(task, PlanTask), f"came back as {type(task).__name__}"
        assert isinstance(task.status, TaskStatus)
        assert task.status is TaskStatus.RUNNING
        # Attribute access is the thing that breaks when it degrades to a dict.
        assert task.action == "retrieve_claims"

        # The live assertion: no type reached the serialiser unregistered.
        unregistered = [
            r.getMessage() for r in caplog.records if "unregistered type" in r.getMessage()
        ]
        assert not unregistered, unregistered
    finally:
        await pool.close()
