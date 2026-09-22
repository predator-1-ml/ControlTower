"""Chat and session endpoints."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command
from sse_starlette.sse import EventSourceResponse

from app.api.schemas import ChatRequest, MessageView, SessionView, TaskView
from app.api.streaming import sse, translate
from app.graph.build import RECURSION_LIMIT
from app.graph.state import new_state
from app.llm.provider import message_text

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

    **An empty message on a resume is an explicit skip**: "I do not have this
    yet". Every pausing node treats an empty answer as nothing supplied, which is
    the only honest reading of it. It is unambiguous on the wire because the
    composer refuses to send an empty message any other way. On a fresh turn an
    empty message is rejected: there is nothing to plan. Rejected: a `skip`
    flag on the request — a second way to say the same thing.
    """
    graph = request.app.state.graph
    deps = request.app.state.deps
    config = _config(body.session_id)

    if body.session_id in _active:
        raise HTTPException(status_code=409, detail="session already has a turn in flight")

    snapshot = await graph.aget_state(config)
    resuming = bool(snapshot.interrupts)
    if not resuming and not body.message.strip():
        raise HTTPException(status_code=422, detail="message is empty and nothing is paused")

    if resuming:
        payload: Any = Command(resume=body.message)
        trace_id = snapshot.values.get("trace_id", str(uuid.uuid4()))
    else:
        trace_id = str(uuid.uuid4())
        # Seed every channel ONLY on a brand-new thread. Graph input is a write
        # like any other: a channel with no reducer is overwritten by it, so
        # seeding on every turn reset `customer_id` and `workflow_states` to empty
        # and the second turn of a session forgot the first. Pinned by
        # `test_second_turn_keeps_the_first_turns_context`.
        seed = {} if snapshot.created_at else new_state(body.session_id, body.user_id, trace_id)
        payload = {
            **seed,
            "messages": [HumanMessage(content=body.message)],
            "trace_id": trace_id,
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
                stream_mode=["updates", "messages"],
                subgraphs=True,
                version="v2",
            )
            known = {t.id: t.status.value for t in snapshot.values.get("plan", [])}
            async for frame in translate(stream, trace_id=trace_id, known=known):
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
        messages=[
            MessageView(
                role="user" if isinstance(m, HumanMessage) else "assistant",
                # message_text, not .content: Gemini returns a list of content
                # blocks there, signature blob included.
                text=message_text(m),
            )
            for m in values.get("messages", [])
            if isinstance(m, HumanMessage | AIMessage)
        ],
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
