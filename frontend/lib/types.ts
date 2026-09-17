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
};
