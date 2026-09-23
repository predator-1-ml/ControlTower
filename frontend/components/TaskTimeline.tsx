"use client";

import { Chip, displayStatus, STATUS, StatusGlyph } from "@/components/Status";
import type { PendingQuestion, Task } from "@/lib/types";

/**
 * The plan, as an activity timeline — the pattern a CRM uses for a deal's
 * history, so an operations handler already knows how to read it.
 *
 * One row per task, in PLAN ORDER, joined by a line. The order never changes:
 * "after t1" always points up the line, and a row that jumped when its status
 * changed would break the one thing a timeline promises. State is carried by the
 * chip (glyph + word + wash), and the one row that needs a person is also the
 * largest and the only tinted one — scale follows urgency.
 *
 * This is also the visible proof of "planning before execution": the list is
 * filled by the `plan` SSE event, which the backend emits the moment the planner
 * returns. Every row lands as Planned at once, before any chip changes.
 *
 * Rejected: a kanban of status columns (the CRM pipeline pattern). Four columns
 * do not fit beside a conversation at 1280px, and three tasks spread over four
 * columns is mostly empty boxes. The owner chose the timeline.
 */

// Workflow identity: a dot beside the printed workflow name — never the only
// carrier. Exported because the Session rows, the turn chips and the empty
// state's groups must all use the same colours as the task rows.
export const WORKFLOW_DOT: Record<Task["workflow"], string> = {
  onboarding: "bg-wf-onboarding",
  claims: "bg-wf-claims",
  knowledge: "bg-wf-knowledge",
};

export function TaskTimeline({
  tasks,
  pending,
  live,
}: {
  tasks: Task[];
  pending: PendingQuestion | null;
  /** A chat stream is open right now. Only then may a row claim to be running. */
  live: boolean;
}) {
  if (tasks.length === 0) {
    // An empty state that teaches the panel instead of saying "nothing here".
    // While a turn is open the plan has been asked for and has not landed yet.
    return live ? (
      <p className="flex items-center gap-2 text-sm font-medium">
        <StatusGlyph glyph="arc" />
        Planning…
      </p>
    ) : (
      <p className="text-sm text-ink-2">
        No plan yet. Send a request and every task appears here as Planned before anything runs.
        A row turns amber when it needs an answer from you.
      </p>
    );
  }

  return (
    <ol>
      {tasks.map((task) => {
        const shown = displayStatus(task, pending, live);
        const status = STATUS[shown];
        const needsYou = shown === "needs_input";
        // "waits for" on a row whose dependencies have finished would be false,
        // so the edge is reworded rather than removed: the dependency is still
        // what makes this a plan and not a to-do list.
        const waiting = task.depends_on.some(
          (id) => tasks.find((other) => other.id === id)?.status !== "done",
        );

        return (
          // The row names itself for the View Transitions API, so a status change
          // crossfades THIS row and slides the ones below it (globals.css,
          // Workspace `move`). Task ids are unique per session, which is exactly
          // the uniqueness the API requires.
          //
          // The joining line is the row's own ::before, from under the id badge
          // to the row's foot; spacing between rows is padding, not a gap, so
          // the line is unbroken. The last row has no line: nothing follows it.
          <li
            key={task.id}
            style={{ viewTransitionName: `task-${task.id}` }}
            className="relative flex gap-3 pb-4 before:absolute before:bottom-0 before:left-[15px] before:top-9 before:w-px before:bg-line last:pb-0 last:before:hidden"
          >
            <span
              className={`figures flex h-8 w-8 shrink-0 items-center justify-center rounded-full border text-xs font-semibold ${
                needsYou ? "border-hold bg-hold-wash text-hold" : "border-line bg-canvas text-ink-2"
              }`}
            >
              {task.id}
            </span>

            <div
              className={`flex min-w-0 flex-1 flex-wrap items-start justify-between gap-x-3 gap-y-1.5 ${
                needsYou ? "rounded-lg bg-hold-wash p-3" : "pt-1"
              }`}
            >
              <div className="min-w-0">
                {/* `action` is the planner's identifier (onboard_customer); shown
                    as words because an operator reads this, not a developer. */}
                <p className={`font-semibold first-letter:uppercase ${needsYou ? "text-base" : "text-sm"}`}>
                  {task.action.replaceAll("_", " ")}
                </p>
                <p className="flex items-center gap-1.5 text-sm text-ink-2">
                  <span aria-hidden="true" className={`h-2 w-2 rounded-full ${WORKFLOW_DOT[task.workflow]}`} />
                  <span className="capitalize">{task.workflow}</span>
                  {task.depends_on.length > 0 &&
                    ` · ${waiting ? "waits for" : "after"} ${task.depends_on.join(", ")}`}
                </p>
                {/* Printed in the row, not in a tooltip: a recording cannot hover. */}
                {task.error && <p className="mt-1 text-sm text-fail">{task.error}</p>}
              </div>
              <Chip glyph={status.glyph} tone={status.tone}>
                {status.label}
              </Chip>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
