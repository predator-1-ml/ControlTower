import type { PendingQuestion, Task, TaskStatus } from "@/lib/types";

/**
 * The status vocabulary: one place that turns a state into glyph + label +
 * colour, and the one chip that draws it.
 *
 * It is its own file because it has four callers — the task rows, the
 * sidebar's connection status, the notice bar and the question card. Written
 * four times, the mappings drift, and the sidebar ends up calling "paused" one
 * colour while the row calls it another.
 *
 * Status is never colour-only: every use renders a glyph, a printed label, and
 * colour. A screen recording's compression flattens pastel pills into one grey;
 * a word and a shape survive it.
 */

/** Wire statuses plus the one the browser can only infer. */
export type DisplayStatus = TaskStatus | "stalled";

/**
 * What a task row should SAY, which is not always what the backend sent.
 *
 * The supervisor only ever writes `running`; an `interrupt()` leaves the task
 * `running` in the checkpoint because the node never returned. So two states
 * that matter most to a human are indistinguishable on the wire and have to be
 * derived here:
 *
 * - `running` while its workflow has an open question is really "needs input".
 *   Shown as running, the row spun forever while the system was waiting on
 *   the operator — the opposite of the truth.
 * - `running` with no stream open is "last seen running". After a crash the
 *   checkpoint says `running` indefinitely; animating it would claim liveness
 *   this browser has not observed.
 *
 * Rejected: adding `needs_input` writes to the backend. It would mean a state
 * write before `interrupt()`, which re-runs its node on resume (see CLAUDE.md).
 */
export function displayStatus(
  task: Task,
  pending: PendingQuestion | null,
  live: boolean,
): DisplayStatus {
  if (task.status !== "running") return task.status;
  if (pending?.workflow === task.workflow) return "needs_input";
  return live ? "running" : "stalled";
}

export type Glyph = "circle" | "arc" | "half" | "pause" | "check" | "cross" | "dash";

/** The chip's wash + text pair. Five roles cover nine statuses. */
export const TONE = {
  quiet: "bg-sunken text-ink-2",
  // Running work is ink, not coloured: colour is reserved for the three things
  // that are news — needs you, done, failed.
  busy: "bg-sunken text-ink",
  // Filled, unlike the others: a filled chip always means "look", and it has to
  // stay visible on the amber-washed row it sits in.
  hold: "bg-hold text-surface",
  clear: "bg-clear-wash text-clear",
  fail: "bg-fail-wash text-fail",
} as const;

export const STATUS: Record<DisplayStatus, { label: string; tone: string; glyph: Glyph }> = {
  pending: { label: "Planned", tone: TONE.quiet, glyph: "circle" },
  ready: { label: "Ready", tone: TONE.quiet, glyph: "circle" },
  blocked: { label: "Blocked", tone: TONE.quiet, glyph: "circle" },
  running: { label: "Running", tone: TONE.busy, glyph: "arc" },
  stalled: { label: "Last seen running", tone: TONE.quiet, glyph: "half" },
  needs_input: { label: "Needs you", tone: TONE.hold, glyph: "pause" },
  done: { label: "Done", tone: TONE.clear, glyph: "check" },
  failed: { label: "Failed", tone: TONE.fail, glyph: "cross" },
  skipped: { label: "Skipped", tone: TONE.quiet, glyph: "dash" },
};

const RING = <circle cx="8" cy="8" r="6.25" />;

// Authored SVG, not Unicode (◐ ✓): those fall back to a different font per OS
// and sit off the baseline. Not an icon package either — seven paths do not
// justify a dependency that then has to be defended.
const PATHS: Record<Glyph, React.ReactNode> = {
  circle: RING,
  arc: <path d="M8 1.75A6.25 6.25 0 1 1 1.75 8" />,
  half: (
    <>
      {RING}
      <path d="M8 1.75a6.25 6.25 0 0 1 0 12.5z" fill="currentColor" />
    </>
  ),
  pause: (
    <>
      {RING}
      <path d="M6.25 5.5v5M9.75 5.5v5" />
    </>
  ),
  check: (
    <>
      {RING}
      <path d="M5.25 8.25l2 2 3.5-4.25" />
    </>
  ),
  cross: (
    <>
      {RING}
      <path d="M5.75 5.75l4.5 4.5M10.25 5.75l-4.5 4.5" />
    </>
  ),
  dash: (
    <>
      {RING}
      <path d="M5.25 8h5.5" />
    </>
  ),
};

/**
 * Always `aria-hidden`: the printed label next to it is the accessible name, so
 * announcing the picture as well would say every status twice.
 *
 * Only the open arc spins, and every caller shows it only while a request is
 * genuinely in flight (a Running row, "Planning…", the sidebar chip while
 * streaming or retrying, the retry notice). globals.css stops the spin under
 * prefers-reduced-motion, which leaves a static arc — still a distinct shape.
 */
export function StatusGlyph({ glyph, className = "" }: { glyph: Glyph; className?: string }) {
  return (
    <svg
      viewBox="0 0 16 16"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`h-4 w-4 shrink-0 ${glyph === "arc" ? "animate-spin" : ""} ${className}`}
    >
      {PATHS[glyph]}
    </svg>
  );
}

/** Glyph + word on a wash. The pill is the one fully round shape in the system:
 *  cards and controls are 8px, so a pill always reads as "a status", never as a
 *  button. */
export function Chip({ glyph, tone, children }: { glyph: Glyph; tone: string; children: React.ReactNode }) {
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ${tone}`}
    >
      <StatusGlyph glyph={glyph} />
      {children}
    </span>
  );
}
