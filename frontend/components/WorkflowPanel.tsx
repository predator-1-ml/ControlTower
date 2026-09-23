"use client";

import { useEffect, useRef } from "react";

import { displayStatus, STATUS, StatusGlyph } from "@/components/Status";
import { TaskTimeline, WORKFLOW_DOT } from "@/components/TaskTimeline";
import type { PendingQuestion, Task, WorkflowStates } from "@/lib/types";

/** One line of the activity log. `at` is this browser's clock, HH:MM:SS. */
export type LogEntry = { at: string; text: string };

// One card: white surface, hairline, the single shadow. Three uses, all here.
const CARD = "rounded-lg border border-line bg-surface p-4 shadow-card";

// The brief's order, and the order the demo runs them in. Fixed, not derived
// from the plan: a row that is "Not used yet" is the invitation to use it.
const WORKFLOWS = ["onboarding", "claims", "knowledge"] as const;

const words = (value: string) => value.replaceAll("_", " ");

/**
 * What a workflow last concluded, in a handler's words, from the `outcome` each
 * graph writes when it finishes. A lookup, not a template engine: one line per
 * outcome, and the ONE fact beside it is chosen here, in the open. `next_actions`
 * are full sentences with citations and stay in the answer.
 *
 * An outcome missing from this map prints as words rather than nothing: a blank
 * row on a workflow that visibly ran would read as a bug.
 */
function outcomeText(workflow: (typeof WORKFLOWS)[number], states: WorkflowStates): string | null {
  const outcome = states[workflow]?.outcome;
  if (!outcome) return null;

  if (workflow === "onboarding") {
    const ws = states.onboarding ?? {};
    if (outcome === "application_created") return "Application created";
    if (outcome === "manual_review") return `Manual review · ${ws.review_reason ?? ""}`.trimEnd();
    if (outcome === "rejected") return `Rejected · ${(ws.reasons ?? []).join(", ")}`;
    if (outcome === "customer_not_found") return "Customer not found";
  }
  if (workflow === "claims") {
    const ws = states.claims ?? {};
    if (outcome === "summarised")
      return (ws.claims ?? []).map((c) => `${c.claim_ref} ${words(c.status)}`).join(", ");
    if (outcome === "information_incomplete")
      return `Still awaiting: ${(ws.missing_fields ?? []).map(words).join(", ")}`;
    if (outcome === "no_claims") return "No open claims";
    if (outcome === "not_found") return "Customer or claim not found";
    if (outcome === "registered")
      return `${(ws.claims ?? []).map((c) => c.claim_ref).join(", ")} registered`;
    if (outcome === "not_registered") return "Not registered";
  }
  if (workflow === "knowledge") {
    // `citations` is every passage retrieved, not the ones the answer quotes —
    // the same count the trail reports as "passages found".
    const passages = states.knowledge?.citations?.length ?? 0;
    if (outcome === "answered") return `Answered from ${passages} passage${passages === 1 ? "" : "s"}`;
    if (outcome === "no_matching_policy") return "No policy document covers it";
  }
  return words(outcome);
}

/**
 * Who this session is about, from what the workflows recorded. Onboarding keeps
 * the customer it loaded; every claim row carries its customer, whichever way
 * it was reached (all claim queries join the customer table).
 */
function contextText(states: WorkflowStates): string {
  const onboarding = states.onboarding;
  const claims = states.claims?.claims ?? [];
  const known = claims.find((c) => c.customer_ref);

  const customer = onboarding?.customer
    ? `${onboarding.customer.external_ref} ${onboarding.customer.full_name}`
    : onboarding?.customer_ref
      ? `${onboarding.customer_ref}, not found`
      : known
        ? `${known.customer_ref} ${known.customer_name ?? ""}`.trimEnd()
        : null;
  const refs = claims.map((c) => c.claim_ref).join(", ");

  if (!customer && !refs) return "No customer identified yet.";
  return [customer && `Customer ${customer}`, refs && `Claims ${refs}`].filter(Boolean).join(" · ");
}

/**
 * The right-hand column: the session, the plan, the activity log, and the ids.
 *
 * The Session card is the coverage view: three fixed rows, one per workflow the
 * brief names, each with what that workflow last concluded. It replaced a
 * sidebar legend that counted tasks per workflow and was hidden on a phone.
 * Rejected: one tab per workflow (hides two of three, the opposite of showing
 * coverage) and structured record cards inside the conversation (a new payload
 * to defend, and lost on restore).
 *
 * The log is written by the workspace from the SSE events it receives (`plan`,
 * `task`, `interrupt`, `final`, `error`) plus what the browser itself did (sent,
 * lost the stream, restored). Without it a long multi-workflow run looks
 * identical to a hung one. Identical timestamps on `plan` and the first
 * `→ running` line are the on-screen proof that planning came first.
 *
 * It is browser-only and not replayed after a restore — the durable record is
 * `audit_events`, joined by the trace id at the foot.
 */
export function WorkflowPanel({
  tasks,
  states,
  pending,
  live,
  connecting,
  activity,
  sessionId,
  traceId,
}: {
  tasks: Task[];
  states: WorkflowStates;
  pending: PendingQuestion | null;
  live: boolean;
  /** A stored session is still being restored: nothing below is known yet. */
  connecting: boolean;
  activity: LogEntry[];
  sessionId: string;
  traceId: string | null;
}) {
  /**
   * A row is the LATEST task of its workflow, summarised. Reusing displayStatus
   * keeps the vocabulary identical to the timeline underneath: this row cannot
   * say Running while the task row says Last seen running. Only a finished task
   * reads its outcome from the checkpoint, so a workflow re-run in a later turn
   * never shows its earlier conclusion while it works, and a crash-restored
   * session (task `running`, no stream, no outcome yet) says so rather than
   * "Not used yet". Until the end-of-turn refetch lands, a done task falls back
   * to the plain "Done".
   */
  function rowText(workflow: (typeof WORKFLOWS)[number]): { text: string; running: boolean } {
    const latest = tasks.filter((task) => task.workflow === workflow).at(-1);
    if (!latest) return { text: "Not used yet", running: false };
    const shown = displayStatus(latest, pending, live);
    if (shown === "done") return { text: outcomeText(workflow, states) ?? STATUS.done.label, running: false };
    return { text: STATUS[shown].label, running: shown === "running" };
  }
  // Newest entry is last, so keep the log pinned to its end; otherwise the line
  // that explains what just happened is the one below the fold.
  const logRef = useRef<HTMLOListElement>(null);
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [activity]);

  const done = tasks.filter((task) => task.status === "done").length;

  return (
    // `contents` below lg: on a phone the aside's children join the page's own
    // column, so `order` can put the plan ABOVE the conversation and the log
    // below it — the plan is short and it is the evidence; under the transcript
    // it would never be seen. On desktop the aside is an ordinary right-hand pane.
    <aside className="contents lg:flex lg:min-h-0 lg:flex-col lg:gap-4 lg:overflow-y-auto lg:border-l lg:border-line lg:p-4">
      {/* Its own card with its own heading, before Plan in the DOM and sharing
          its order: a small label above the Plan heading would be a kicker, and
          a card inside the Plan card a nested one (DESIGN.md §9). Rows are plain
          text, not status chips: the row is a fact line, the timeline below is
          the status machine, and a second amber element in the column would
          dilute the one that needs a person. */}
      <section aria-label="Session" className={`order-1 m-4 mb-0 lg:m-0 ${CARD}`}>
        <h2 className="font-semibold">Session</h2>
        {connecting ? (
          <p className="mt-2 flex items-center gap-2 text-sm text-ink-2">
            <StatusGlyph glyph="arc" />
            Connecting…
          </p>
        ) : (
          <>
            <p className="mt-1 text-sm text-ink-2">{contextText(states)}</p>
            <ul aria-label="Workflows in this session" className="mt-3 space-y-2 border-t border-line pt-3 text-sm">
              {WORKFLOWS.map((workflow) => {
                const row = rowText(workflow);
                return (
                  <li key={workflow} className="flex items-start gap-2">
                    <span aria-hidden="true" className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${WORKFLOW_DOT[workflow]}`} />
                    <span className="w-24 shrink-0 font-medium capitalize">{workflow}</span>
                    <span className="flex min-w-0 items-baseline gap-1.5 text-ink-2">
                      {row.running && <StatusGlyph glyph="arc" className="h-3.5 w-3.5 self-center" />}
                      <span className="break-words">{row.text}</span>
                    </span>
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </section>

      <section aria-label="Plan" className={`order-1 m-4 lg:m-0 ${CARD}`}>
        <header className="mb-4 flex items-baseline justify-between gap-3">
          <h2 className="font-semibold">Plan</h2>
          {tasks.length > 0 && (
            <p className="figures text-sm text-ink-2">
              {done} of {tasks.length} done
            </p>
          )}
        </header>
        <TaskTimeline tasks={tasks} pending={pending} live={live} />
      </section>

      {/* Native <details>: collapse, keyboard support and the open/closed state
          all come from the browser — no React state to explain. */}
      <details open className={`order-3 m-4 lg:m-0 ${CARD}`}>
        <summary className="cursor-pointer font-semibold">Activity</summary>
        {activity.length === 0 ? (
          <p className="mt-2 text-sm text-ink-2">
            Nothing logged in this tab yet. Each event is timed as it arrives.
          </p>
        ) : (
          <ol ref={logRef} className="mt-2 max-h-64 space-y-1 overflow-y-auto text-sm">
            {activity.map((entry, index) => (
              <li key={index} className="flex gap-3">
                <time className="figures shrink-0 text-ink-3">{entry.at}</time>
                <span className="min-w-0 text-ink-2">{entry.text}</span>
              </li>
            ))}
          </ol>
        )}
      </details>

      {/* Full length and select-all, deliberately: docs/demo.md has the presenter
          paste the trace id into SQL to join a run to its audit_events rows. */}
      <footer className="figures order-4 mt-auto px-4 pb-4 text-xs text-ink-2 lg:px-1 lg:pb-0">
        <p>
          session <span className="select-all break-all">{sessionId}</span>
        </p>
        {traceId && (
          <p>
            trace <span className="select-all break-all">{traceId}</span>
          </p>
        )}
      </footer>
    </aside>
  );
}
