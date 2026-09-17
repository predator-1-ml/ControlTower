"""Chat and session endpoints."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from sse_starlette.sse import EventSourceResponse

from app.api.schemas import ChatRequest, SessionView, TaskView
from app.api.streaming import sse, translate
from app.graph.build import RECURSION_LIMIT

router = APIRouter()

#: Sessions with a turn in flight.
#:
#: Two concurrent turns on one thread_id race the checkpointer and interleave
#: writes. Rejecting the second is the honest fix at this scale. It is
#: deliberately in-process, which means it only holds for a single backend task —
#: a correct implementation would take a Postgres advisory lock keyed on the
#: session. Documented as a known limitation rather than half-solved.
_active: set[str] = set()


def _config(session_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": session_id},
        # Default is 25; a supervisor loop costs 2 super-steps per task, so the
        # default dies at ~11 tasks with GraphRecursionError.
        "recursion_limit": RECURSION_LIMIT,
    }


@router.post("/chat")
async def chat(request: Request, body: ChatRequest) -> EventSourceResponse:
    """Advance a session by one turn, streaming progress as SSE.

    **Resume takes precedence over planning.** If the session is parked on an
    `interrupt()`, the incoming message is the human's answer and is delivered as
    `Command(resume=...)`. Running the planner instead would re-plan over a paused
    workflow and discard the answer the user just typed — the single most
    destructive ordering mistake available here.
    """
    graph = request.app.state.graph
    deps = request.app.state.deps
    config = _config(body.session_id)

    if body.session_id in _active:
        raise HTTPException(status_code=409, detail="session already has a turn in flight")

    snapshot = await graph.aget_state(config)
    resuming = bool(snapshot.interrupts)

    if resuming:
        payload: Any = Command(resume=body.message)
        trace_id = snapshot.values.get("trace_id", str(uuid.uuid4()))
    else:
        trace_id = str(uuid.uuid4())
        payload = {
            "messages": [HumanMessage(content=body.message)],
            "session_id": body.session_id,
            "user_id": body.user_id,
            "trace_id": trace_id,
            # Seed the channels a brand-new thread needs. On an existing thread
            # the checkpointer already holds these and they are ignored.
            "plan": [],
            "workflow_states": {},
            "tool_results": [],
            "errors": [],
            "customer_id": None,
            "current_intent": None,
            "active_workflow": None,
            "pending_question": None,
            "final_response": None,
            "schema_version": 1,
        }

    async def events():
        _active.add(body.session_id)
        try:
            stream = graph.astream(
                payload,
                config,
                context=deps,
                # No "values": interrupts arrive on "updates", and streaming full
                # state snapshots would push the entire plan over the wire on
                # every super-step for nothing.
                stream_mode=["updates", "messages", "custom"],
                subgraphs=True,
                version="v2",
            )
            async for frame in translate(stream, trace_id=trace_id):
                yield frame
            yield sse("done", {"trace_id": trace_id})
        except Exception as exc:  # noqa: BLE001 - must reach the client, not a 500 page
            # The response has already begun streaming, so an exception here
            # cannot become an HTTP error code. Deliver it as an event or the
            # client sees a truncated stream with no explanation.
            yield sse("error", {"trace_id": trace_id, "kind": "graph_failed",
                                "message": str(exc)[:500]})
        finally:
            _active.discard(body.session_id)

    return EventSourceResponse(
        events(),
        # Heartbeat well inside the ALB's 300s idle timeout. Any byte in either
        # direction resets it, so this alone keeps a long run's connection alive.
        ping=15,
        headers={
            "Cache-Control": "no-cache, no-store, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sessions/{session_id}", response_model=SessionView)
async def get_session(request: Request, session_id: str) -> SessionView:
    """Current session state, for a client reconnecting or refreshing.

    SSE has no replay, so a client that drops mid-run needs this to redraw the
    plan and discover whether a question is waiting on it.
    """
    snapshot = await request.app.state.graph.aget_state(_config(session_id))
    if not snapshot.created_at:
        raise HTTPException(status_code=404, detail="unknown session")

    values = snapshot.values
    pending = snapshot.interrupts[0].value if snapshot.interrupts else None

    return SessionView(
        session_id=session_id,
        status="awaiting_input" if snapshot.interrupts else "idle",
        plan=[
            TaskView(
                id=t.id,
                workflow=t.workflow,
                action=t.action,
                status=t.status.value,
                depends_on=t.depends_on,
                error=t.error,
            )
            for t in values.get("plan", [])
        ],
        workflow_states=values.get("workflow_states", {}),
        final_response=values.get("final_response"),
        pending_question=pending,
    )
