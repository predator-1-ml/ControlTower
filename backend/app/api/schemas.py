"""Request and response shapes for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    session_id: str = Field(description="Also the checkpointer thread_id. Stable per conversation.")
    message: str
    user_id: str = "operator"


class TaskView(BaseModel):
    """A plan task, flattened for the UI."""

    id: str
    workflow: str
    action: str
    status: str
    depends_on: list[str]
    error: str | None = None


class MessageView(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class SessionView(BaseModel):
    """Everything a reconnecting client needs to redraw the session."""

    session_id: str
    status: Literal["idle", "awaiting_input"]
    # The transcript, because SSE has no replay: a browser that refreshes has
    # lost every token it was sent, and the checkpoint is the only copy left.
    messages: list[MessageView]
    plan: list[TaskView]
    workflow_states: dict[str, Any]
    final_response: str | None = None
    pending_question: dict[str, Any] | None = None
