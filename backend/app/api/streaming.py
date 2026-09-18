"""Translate LangGraph's stream into a stable SSE contract for the frontend.

Kept separate from the route so the mapping is testable without HTTP, and so the
client contract has one obvious place to read.

The event names below are the API. LangGraph's internal chunk shapes are not —
they change between versions, and a frontend built directly on them breaks on
upgrade. Everything crossing the wire is one of:

    plan       the task DAG, as soon as the planner produces it
    task       a task changed status
    token      a fragment of assistant text
    interrupt  the graph paused and needs a human
    final      the composed answer
    error      something failed

`version="v2"` is what makes this tractable: it gives every chunk the same
`{type, ns, data}` shape regardless of stream_mode, so there is no shape-sniffing
here. v1 varies its shape by call, and `astream_events(version="v3")` is nicer
still for interrupts but is flagged beta — a poor foundation for a contract the
frontend depends on.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

#: Nodes whose token stream is user-facing. The planner and supervisor also call
#: models (or would), and streaming their internals to the chat pane would be
#: noise at best and confusing at worst.
STREAMING_NODES = {"compose", "generate", "summarise"}


def sse(event: str, payload: dict[str, Any]) -> dict[str, str]:
    """One SSE frame. sse-starlette takes dicts with event/data keys."""
    return {"event": event, "data": json.dumps(payload, default=str)}


def _task_view(task: Any) -> dict[str, Any]:
    return {
        "id": task.id,
        "workflow": task.workflow,
        "action": task.action,
        "status": task.status.value,
        "depends_on": task.depends_on,
        "error": task.error,
    }


async def translate(
    chunks: AsyncIterator[dict[str, Any]], *, trace_id: str
) -> AsyncIterator[dict[str, str]]:
    """Map LangGraph stream parts to SSE frames.

    `trace_id` rides on every frame so a failure a user reports can be joined
    straight to its `audit_events` rows. Without it, correlating a complaint to a
    run means guessing from timestamps.
    """
    seen_status: dict[str, str] = {}
    seen_interrupts: set[str] = set()
    announced_plan = False

    async for chunk in chunks:
        kind = chunk.get("type")
        data = chunk.get("data")

        if kind == "messages":
            message, meta = data
            if meta.get("langgraph_node") in STREAMING_NODES:
                text = getattr(message, "text", None) or getattr(message, "content", "")
                if text:
                    yield sse("token", {"trace_id": trace_id, "text": text})

        elif kind == "custom":
            yield sse("progress", {"trace_id": trace_id, **(data or {})})

        elif kind == "updates":
            for node, update in (data or {}).items():
                # Interrupts arrive HERE, on the updates channel, keyed
                # `__interrupt__` — not on `values` as one might expect, and the
                # value is a tuple rather than a state dict. An `isinstance(...,
                # dict)` guard silently swallows them, which is exactly the bug
                # this branch exists to prevent: the graph pauses, the client
                # sees the stream simply end, and the user is left waiting for a
                # question that was never delivered.
                if node == "__interrupt__":
                    for item in update or ():
                        # `subgraphs=True` reports one interrupt twice: once in
                        # the subgraph's namespace and again as it propagates
                        # through the parent. Same id both times, so the id is
                        # what makes it one question rather than two.
                        if item.id in seen_interrupts:
                            continue
                        seen_interrupts.add(item.id)
                        yield sse(
                            "interrupt",
                            {"trace_id": trace_id, "id": item.id, **(item.value or {})},
                        )
                    continue

                if not isinstance(update, dict):
                    continue

                # The plan DAG, emitted the moment the planner produces it. This
                # is what makes "planning before execution" visible rather than
                # merely true.
                plan = update.get("plan")
                if plan and node == "planner" and not announced_plan:
                    announced_plan = True
                    yield sse(
                        "plan",
                        {"trace_id": trace_id, "tasks": [_task_view(t) for t in plan]},
                    )

                # Status transitions, deduplicated: the supervisor rewrites the
                # same task several times per loop and the UI should not flicker.
                for task in plan or []:
                    if seen_status.get(task.id) != task.status.value:
                        seen_status[task.id] = task.status.value
                        yield sse("task", {"trace_id": trace_id, **_task_view(task)})

                for error in update.get("errors") or []:
                    yield sse("error", {"trace_id": trace_id, **error})

                if update.get("final_response"):
                    yield sse(
                        "final",
                        {"trace_id": trace_id, "response": update["final_response"]},
                    )
