"""Translate LangGraph's stream into a stable SSE contract for the frontend.

Kept separate from the route so the mapping is testable without HTTP, and so the
client contract has one obvious place to read.

The event names below are the API. LangGraph's internal chunk shapes are not —
they change between versions, and a frontend built directly on them breaks on
upgrade. Everything crossing the wire is one of:

    plan       the task DAG, as soon as the planner produces it
    task       a task changed status
    step       a graph node the handler would recognise has finished
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

#: What each node is called when a handler describes it. Keyed by
#: `(workflow, node)` because `retrieve`, `validate` and `not_found` each exist in
#: two subgraphs and mean different things in each.
#:
#: **The labels are mapped here, in the backend, so a LangGraph node name never
#: crosses the wire** — the module rule above: the event names are the API, the
#: chunk shapes are not. An unmapped node emits nothing, which is also how nodes
#: a handler has no use for (`embed`) stay out of the trail.
#:
#: The pausing nodes are deliberately absent. Their update is emitted when the
#: node *finishes*, which for a node holding an `interrupt()` is on the RESUME
#: turn — so a label here would appear one turn late, under the answer instead of
#: above the question. "Needs your answer" is added by the client from the
#: `interrupt` event it already receives.
STEP_LABELS: dict[tuple[str, str], str] = {
    ("claims", "retrieve"): "looked up the claim",
    ("claims", "validate"): "checked it for missing information",
    ("claims", "not_found"): "found nothing to report",
    ("claims", "read_reply"): "read your reply",
    ("claims", "record_information"): "updated the claim",
    ("claims", "assess"): "applied the handling rules",
    ("claims", "summarise"): "wrote the claim summary",
    ("claims", "prepare"): "checked who the claim is for",
    ("claims", "read_details"): "read your reply",
    ("claims", "create"): "registered the claim",
    ("claims", "not_registered"): "did not register the claim",
    ("knowledge", "retrieve"): "searched policy documents",
    ("knowledge", "generate"): "answered from the passages found",
    ("knowledge", "no_results"): "found no matching policy",
    ("onboarding", "load_customer"): "looked up the customer",
    ("onboarding", "not_found"): "found no such customer",
    ("onboarding", "validate"): "checked the customer record",
    ("onboarding", "verify_identity"): "checked identity verification",
    ("onboarding", "check_eligibility"): "applied the eligibility rules",
    ("onboarding", "create_application"): "created the application",
    ("onboarding", "manual_review"): "sent it to manual review",
    ("onboarding", "reject"): "rejected the application",
    ("", "compose"): "wrote the answer",
}


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
    chunks: AsyncIterator[dict[str, Any]], *, trace_id: str, known: dict[str, str]
) -> AsyncIterator[dict[str, str]]:
    """Map LangGraph stream parts to SSE frames.

    `trace_id` rides on every frame so a failure a user reports can be joined
    straight to its `audit_events` rows. Without it, correlating a complaint to a
    run means guessing from timestamps.
    """
    # Seeded with the statuses the checkpoint ALREADY holds (`known`). A subgraph
    # hands its whole state back when it exits, so every task from earlier turns
    # passes through here again; starting from empty, each one looked new and the
    # client was told "t1 -> done" in the middle of a turn that never touched t1.
    seen_status: dict[str, str] = dict(known)
    seen_interrupts: set[str] = set()
    announced_plan = False

    async for chunk in chunks:
        kind = chunk.get("type")
        data = chunk.get("data")

        if kind == "messages":
            message, meta = data
            node = meta.get("langgraph_node")
            if node in STREAMING_NODES:
                text = getattr(message, "text", None) or getattr(message, "content", "")
                if text:
                    # `node` rides along because a turn can stream from two nodes:
                    # a workflow writes its summary, then `compose` narrates the
                    # rest. Without it the client concatenates the two into one
                    # message and shows an answer glued to the next until `final`
                    # replaces both.
                    yield sse("token", {"trace_id": trace_id, "node": node, "text": text})

        elif kind == "updates":
            # `subgraphs=True` namespaces every subgraph node as
            # ("<workflow>:<uuid>",); top-level nodes carry an empty tuple. That
            # shape is LangGraph's, not ours, so it is pinned by a test.
            ns = chunk.get("ns") or ()
            workflow = ns[0].split(":")[0] if ns else ""

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

                # `updates` arrive when a node FINISHES, so the trail is a list of
                # completed steps — not a claim about what is running now. That is
                # the honest reading of "live" (PRODUCT.md, principle 3: claim only
                # what was observed).
                label = STEP_LABELS.get((workflow, node))
                if label:
                    yield sse("step", {"trace_id": trace_id, "label": label})

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

                # From `compose` ONLY. `final_response` is ordinary state, so a
                # workflow subgraph exiting mid-turn carries LAST turn's answer in
                # its update — relayed as `final`, the client replaced the answer
                # it was streaming with the previous one.
                if node == "compose" and update.get("final_response"):
                    yield sse(
                        "final",
                        {"trace_id": trace_id, "response": update["final_response"]},
                    )
