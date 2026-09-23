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
  /**
   * Which workflows ran in this turn, first seen first. Same provenance and
   * the same caveat as `steps`: from the `plan` and `task` events this browser
   * received, so a restored turn has none.
   */
  workflows?: Task["workflow"][];
};

/**
 * The slice each workflow keeps in the checkpoint (`workflow_states`, keyed by
 * workflow). The backend types it `dict[str, Any]`; only the keys the Session
 * card reads are named here, and every one is optional because a slice is
 * REPLACED when its workflow starts (`retrieve`, `load_customer`) — mid-run it
 * has no `outcome`, and after `customer_not_found` its `customer` is null.
 */
export type WorkflowStates = {
  onboarding?: {
    customer?: { external_ref: string; full_name: string } | null;
    customer_ref?: string;
    outcome?: string;
    review_reason?: string;
    reasons?: string[];
  };
  claims?: {
    claims?: { claim_ref: string; status: string; customer_ref?: string; customer_name?: string }[];
    missing_fields?: string[];
    outcome?: string;
  };
  knowledge?: {
    outcome?: string;
    citations?: { source: string; section: string }[];
  };
};

/** GET /sessions/{id} — what a refreshed page redraws itself from. */
export type SessionView = {
  session_id: string;
  status: "idle" | "awaiting_input";
  messages: ChatMessage[];
  plan: Task[];
  workflow_states: WorkflowStates;
  pending_question: PendingQuestion | null;
};
