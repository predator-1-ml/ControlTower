"use client";

import type { Task, TaskStatus } from "@/lib/types";

/**
 * The plan, rendered as a dependency list.
 *
 * This component is the visible proof of "planning before execution": it is
 * populated by the `plan` SSE event, which the backend emits the moment the
 * planner returns — before any workflow has run. If it only appeared alongside
 * the final answer, the requirement would be satisfied in the code and
 * invisible in the product.
 */

const STATUS_STYLE: Record<TaskStatus, string> = {
  pending: "bg-slate-100 text-slate-600 border-slate-200",
  ready: "bg-slate-100 text-slate-600 border-slate-200",
  blocked: "bg-amber-50 text-amber-700 border-amber-200",
  running: "bg-blue-50 text-blue-700 border-blue-300 animate-pulse",
  needs_input: "bg-amber-50 text-amber-800 border-amber-300",
  done: "bg-emerald-50 text-emerald-700 border-emerald-200",
  failed: "bg-red-50 text-red-700 border-red-200",
  skipped: "bg-slate-50 text-slate-400 border-slate-200 line-through",
};

const WORKFLOW_DOT: Record<Task["workflow"], string> = {
  onboarding: "bg-violet-500",
  claims: "bg-sky-500",
  knowledge: "bg-teal-500",
};

export function TaskTimeline({ tasks }: { tasks: Task[] }) {
  if (tasks.length === 0) {
    return (
      <p className="text-sm text-slate-400">
        No plan yet. Ask something to see the tasks appear before they run.
      </p>
    );
  }

  return (
    <ol className="space-y-2">
      {tasks.map((task) => (
        <li
          key={task.id}
          className={`rounded-md border px-3 py-2 text-sm ${STATUS_STYLE[task.status]}`}
        >
          <div className="flex items-center gap-2">
            <span className={`h-2 w-2 shrink-0 rounded-full ${WORKFLOW_DOT[task.workflow]}`} />
            <span className="font-mono text-xs opacity-60">{task.id}</span>
            <span className="font-medium">{task.action}</span>
            <span className="ml-auto text-xs uppercase tracking-wide opacity-70">
              {task.status.replace("_", " ")}
            </span>
          </div>

          {task.depends_on.length > 0 && (
            // The dependency edge is the whole point of showing a DAG rather
            // than a list — it is what distinguishes a plan from a to-do list.
            <p className="mt-1 pl-4 text-xs opacity-60">
              waits for {task.depends_on.join(", ")}
            </p>
          )}

          {task.error && <p className="mt-1 pl-4 text-xs">{task.error}</p>}
        </li>
      ))}
    </ol>
  );
}
