"""Compose the final answer from what the workflows actually produced.

The model is given the recorded outcomes and told to report them. It is not asked
to decide anything — every decision was already made deterministically upstream,
and re-opening those decisions here is how a system starts confidently
contradicting its own audit trail.

Partial success is the normal case, not an edge case: an operations request that
half-succeeds is a Tuesday. The prompt says so explicitly, because a model given
a mixed result will otherwise narrate the successful half and quietly drop the
rest.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from app.graph.deps import Deps
from app.graph.state import ControlTowerState, TaskStatus
from app.llm.provider import message_text

SYSTEM_PROMPT = """You report the outcome of operations work to a handler.

You are given the tasks that ran and what each produced. Report what happened,
in plain language, in a few sentences.

Rules:
- Report only what the results below state. Do not infer, soften, or embellish.
- If something failed or was skipped, say so and say why. A partially completed
  request is normal; never report only the parts that worked.
- If a task is waiting on information from the user, say clearly what is needed.
- No preamble. Do not begin with "Here is" or "Based on"."""


def _summarise(state: ControlTowerState) -> str:
    lines: list[str] = []
    workflow_states = state.get("workflow_states", {})

    # Report THIS turn's tasks, not the session's. `.get`, and the fall-back to
    # the whole plan, are for threads checkpointed before `turn_task_ids` existed
    # — new state fields must be optional (see state.py, schema_version).
    turn_ids = state.get("turn_task_ids")
    tasks = [t for t in state.get("plan", []) if turn_ids is None or t.id in turn_ids]

    for task in tasks:
        line = f"[{task.id}] {task.workflow}.{task.action} -> {task.status.value}"
        if task.error:
            line += f" (error: {task.error})"
        lines.append(line)

    # Only the workflows this turn touched: every slice outlives its turn, so an
    # untouched one still holds an older request's outcome.
    for workflow in dict.fromkeys(t.workflow for t in tasks):
        ws = workflow_states.get(workflow, {})
        detail = {k: v for k, v in ws.items() if k not in ("customer", "claims")}
        if detail:
            lines.append(f"{workflow} result: {detail}")

    if not lines:
        lines.append("No tasks were planned.")
    return "\n".join(lines)


async def compose_response(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    pending = [t for t in state.get("plan", []) if t.status is TaskStatus.NEEDS_INPUT]

    # HumanMessage, NOT AIMessage. Putting the results in an assistant turn makes
    # the model read them as its own half-finished output and CONTINUE the list —
    # observed inventing `[t2] email.send_onboarding_failed -> skipped` and a
    # fabricated Slack channel id for workflows that do not exist. As a user turn
    # it is data to report on, not a draft to extend.
    response = await runtime.context.model.ainvoke(
        [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=f"Task results:\n{_summarise(state)}"),
        ]
    )
    text = message_text(response)

    return {
        "final_response": text,
        # Append to messages so the next turn has this as conversation context —
        # this is what makes a follow-up like "and the other one?" resolvable.
        "messages": [AIMessage(content=text)],
        "active_workflow": None,
        "pending_question": state.get("pending_question") if pending else None,
    }
