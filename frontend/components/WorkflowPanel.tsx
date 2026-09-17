"use client";

import { TaskTimeline } from "@/components/TaskTimeline";
import type { Task } from "@/lib/types";

/**
 * Right-hand operational view: the plan, plus a live activity log.
 *
 * The log is fed by the `progress` SSE event, which carries per-node updates the
 * graph emits as it runs. Without it a long multi-workflow run looks identical
 * to a hung one — the assignment asks for workflow visibility and task tracking,
 * and "the spinner is still spinning" is neither.
 */
export function WorkflowPanel({
  tasks,
  activity,
  sessionId,
  traceId,
}: {
  tasks: Task[];
  activity: string[];
  sessionId: string;
  traceId: string | null;
}) {
  const done = tasks.filter((task) => task.status === "done").length;

  return (
    <aside className="flex h-full flex-col gap-4 overflow-y-auto">
      <section>
        <header className="mb-2 flex items-baseline justify-between">
          <h2 className="text-sm font-semibold text-slate-700">Plan</h2>
          {tasks.length > 0 && (
            <span className="text-xs text-slate-500">
              {done}/{tasks.length} complete
            </span>
          )}
        </header>
        <TaskTimeline tasks={tasks} />
      </section>

      <section>
        <h2 className="mb-2 text-sm font-semibold text-slate-700">Activity</h2>
        {activity.length === 0 ? (
          <p className="text-sm text-slate-400">Nothing yet.</p>
        ) : (
          <ul className="space-y-1 font-mono text-xs text-slate-600">
            {activity.slice(-12).map((line, index) => (
              <li key={index}>{line}</li>
            ))}
          </ul>
        )}
      </section>

      <footer className="mt-auto border-t border-slate-200 pt-3 font-mono text-[11px] text-slate-400">
        <p>session {sessionId}</p>
        {/* Surfaced deliberately: this is the id that joins a run to its
            audit_events rows, so a problem seen on screen is traceable. */}
        {traceId && <p>trace {traceId}</p>}
      </footer>
    </aside>
  );
}
