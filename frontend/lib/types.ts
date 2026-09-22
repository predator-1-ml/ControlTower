/** Shapes the backend sends. Mirrors backend/app/api/schemas.py. */

export type TaskStatus =
  | "pending"
  | "ready"
  | "running"
  | "blocked"
  | "needs_input"
  | "done"
  | "failed"
  | "skipped";

export type Task = {
  id: string;
  workflow: "onboarding" | "claims" | "knowledge";
  action: string;
  status: TaskStatus;
  depends_on: string[];
  error: string | null;
};

export type PendingQuestion = {
  kind: string;
  workflow: string;
  question: string;
  fields: string[];
};

export type ChatMessage = {
  role: "user" | "assistant";
  text: string;
  /**
   * The step trail for this turn: what this browser watched the graph do.
   * Client-only — it is built from streamed `step` events and is absent from
   * `GET /sessions/{id}`, so a restored turn correctly has none. The durable
   * record of what ran is `audit_events`, joined by trace id.
   */
  steps?: string[];
};

/** GET /sessions/{id} — what a refreshed page redraws itself from. */
export type SessionView = {
  session_id: string;
  status: "idle" | "awaiting_input";
  messages: ChatMessage[];
  plan: Task[];
  pending_question: PendingQuestion | null;
};
