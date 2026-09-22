"use client";

import { useEffect, useRef } from "react";

import { TaskTimeline } from "@/components/TaskTimeline";
import type { PendingQuestion, Task } from "@/lib/types";

/** One line of the activity log. `at` is this browser's clock, HH:MM:SS. */
export type LogEntry = { at: string; text: string };

// One card: white surface, hairline, the single shadow. Two uses, both here.
const CARD = "rounded-lg border border-line bg-surface p-4 shadow-card";

/**
 * The right-hand column: the plan, the activity log, and the ids.
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
  pending,
  live,
  activity,
  sessionId,
  traceId,
}: {
  tasks: Task[];
  pending: PendingQuestion | null;
  live: boolean;
  activity: LogEntry[];
  sessionId: string;
  traceId: string | null;
}) {
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
