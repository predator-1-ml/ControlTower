"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";

import { ChatPanel } from "@/components/ChatPanel";
import { Chip, StatusGlyph, TONE } from "@/components/Status";
import { WORKFLOW_DOT } from "@/components/TaskTimeline";
import { type LogEntry, WorkflowPanel } from "@/components/WorkflowPanel";
import { SendError, streamChat } from "@/lib/sse";
import type { ChatMessage, PendingQuestion, SessionView, Task } from "@/lib/types";

const SESSION_KEY = "control-tower.session";

// 40 tries x 3 s = 2 minutes, which is about one Fargate task replacement
// (deregistration + start + health checks). Long enough to ride out a deploy or
// a crash-restart; short enough that a genuinely dead backend is admitted.
const RETRY_MS = 3000;
const RETRY_LIMIT = 40;

// The secondary button. Not the accent: that is reserved for the one primary
// action on screen, Send (globals.css, rule 2). Disabled is its own grey pair
// (4.9:1), not opacity: a faded label is unreadable in a compressed recording.
const SIDE_BUTTON =
  "w-full rounded-lg border border-line-strong bg-surface px-3 py-1.5 text-sm font-semibold transition-colors duration-150 hover:bg-sunken active:translate-y-px disabled:border-line disabled:bg-sunken disabled:text-ink-3";

const WORKFLOWS = ["onboarding", "claims", "knowledge"] as const;

// "1 tasks" on screen reads as carelessness, and it is on screen in every beat.
const count = (n: number, noun: string) => `${n} ${noun}${n === 1 ? "" : "s"}`;

/**
 * What this browser has OBSERVED about the backend — never what it assumes.
 * "streaming" and "paused" are not here on purpose: they are derived from
 * `busy` and `pending`, and storing them twice is how they would disagree.
 */
type Conn = "connecting" | "ready" | "retrying" | "unreachable";

/** The one transient surface. Transport problems speak here, never as the assistant. */
type Notice = { tone: "ok" | "wait" | "fail"; title: string; body: string };

const NOTICE_TONE = {
  ok: { glyph: "check", style: "bg-clear-wash text-clear" },
  wait: { glyph: "arc", style: "bg-hold-wash text-hold" },
  fail: { glyph: "cross", style: "bg-fail-wash text-fail" },
} as const;

/**
 * Apply a state change to the plan, and let the browser animate it (View
 * Transitions API; the rows name themselves in TaskTimeline, the timing is in
 * globals.css). A row's status crossfades in place, and the rows below slide
 * when the needs-you row grows.
 *
 * `flushSync` is required, not decoration: the API snapshots the DOM, runs this
 * callback, then snapshots again — so React must have committed by the time the
 * callback returns, and a normal setState is batched until later.
 *
 * Without the API (Firefox, older Safari) or with reduced motion, the change is
 * simply applied: the chip's word is the state, the movement only confirms it.
 */
function move(change: () => void) {
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!document.startViewTransition || still) return change();
  // Task events arrive in bursts (pending -> running within milliseconds). A new
  // transition skips the one still running, and the skipped one REJECTS its
  // promises — expected here, so it is swallowed rather than left to surface as
  // an unhandled rejection. The state change itself has already been applied.
  const transition = document.startViewTransition(() => flushSync(change));
  transition.ready.catch(() => {});
  transition.finished.catch(() => {});
}

/** The cookie expired or was never there (proxy.ts answers /bff/* with 401). */
function toSignIn() {
  window.location.assign("/sign-in");
}

/**
 * Single operational screen in a CRM shell: sidebar, conversation, then the plan
 * and activity log. State lives here because both panes are driven by the same
 * event stream, and threading it through a store would add indirection without
 * removing any coupling.
 */
export function Workspace({ operator }: { operator: string }) {
  // Stable for the tab's lifetime. This is the checkpointer's thread_id, so it
  // is also what makes a workflow resumable — including after the backend has
  // been restarted underneath it.
  //
  // Generated in an effect, NOT inline, and that is load-bearing. This component
  // is server-rendered first and then hydrated; `crypto.randomUUID()` called
  // during render produces a different value on each side, the ids disagree in
  // the rendered HTML, and React discards the whole tree and regenerates it:
  //
  //   "Hydration failed because the server rendered text didn't match the client"
  //
  // Effects run only on the client, so the server renders an empty id, hydration
  // matches, and the id is filled in immediately afterwards.
  const [sessionId, setSessionId] = useState("");

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [activity, setActivity] = useState<LogEntry[]>([]);
  const [pending, setPending] = useState<PendingQuestion | null>(null);
  const [traceId, setTraceId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [conn, setConn] = useState<Conn>("connecting");
  const [notice, setNotice] = useState<Notice | null>(null);
  // Lifted out of ChatPanel so a send that got no answer can put the text back
  // in the box instead of making the operator retype it.
  const [draft, setDraft] = useState("");

  // Client clock, and the log says so by being browser-only: it is a record of
  // what THIS tab saw, not an audit trail (that is `audit_events`, by trace id).
  // Capped at 50 so a long session cannot grow the DOM without bound.
  const log = useCallback((text: string) => {
    const at = new Date().toLocaleTimeString("en-GB");
    setActivity((prev) => [...prev, { at, text }].slice(-50));
  }, []);

  // Only the newest restore loop may write state. Without this, "New session"
  // during a retry loop lets the old loop land the OLD session's messages in the
  // new one 3 s later — and React StrictMode, which runs the mount effect twice
  // in dev, would run two loops side by side.
  const restoreRun = useRef(0);

  /**
   * Redraw the screen from the Postgres checkpoint.
   *
   * SSE has no replay, so everything streamed before a refresh or a dropped
   * connection is gone from the browser; the checkpoint is the only copy. ONE
   * function serves refresh, a dropped stream and a failed send, so crash
   * recovery and "I pressed F5" are the same code path — there is no second,
   * rarely-exercised recovery branch to rot.
   *
   * `lost` is the headline to show while retrying (what the operator lost);
   * absent on a plain refresh. `sawOutage` records that a request has actually
   * failed: the notice only says "Backend is back" if this browser watched it go
   * away. A stream can drop for reasons that are not a crash (an idle timeout, a
   * proxy), and claiming a restart we did not observe would be a guess.
   */
  const restore = useCallback(
    async (id: string, lost?: string, sawOutage = false) => {
      const run = ++restoreRun.current;

      for (let attempt = 1; attempt <= RETRY_LIMIT; attempt++) {
        const response = await fetch(`/bff/sessions/${id}`).catch(() => null);
        if (run !== restoreRun.current) return;
        const status = response?.status ?? 0;

        // 0 = the fetch itself failed (Next is down or the network is). 502 is
        // what the BFF returns when it cannot reach the backend; 503/504 are what
        // the internal ALB returns while a task is being replaced. All mean "not
        // there YET". The old code treated them like 404 and drew the empty
        // first-load screen — a down backend looked exactly like a lost session.
        if (status === 0 || status >= 502) {
          sawOutage = true;
          setConn("retrying");
          // No attempt counter in the copy: this is a live region, and a number
          // that changes every 3 s would be read aloud forty times.
          setNotice({
            tone: "wait",
            title: lost ?? "Cannot reach the backend.",
            body: "Retrying every 3 s for up to 2 minutes. Work already checkpointed is safe.",
          });
          await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
          if (run !== restoreRun.current) return;
          continue;
        }

        if (status === 401) return toSignIn();
        setConn("ready");

        // 404 = the backend is up and has never checkpointed this id. Leave the
        // screen alone; on a fresh tab that is simply an empty session.
        if (status === 404) {
          setNotice(
            sawOutage
              ? {
                  tone: "ok",
                  title: "Backend is back.",
                  body: "Nothing had been checkpointed for this session yet, so there is nothing to restore. Send again.",
                }
              : null,
          );
          return;
        }

        if (!response?.ok) {
          setNotice({ tone: "fail", title: `Request failed (${status}).`, body: "Nothing was changed." });
          return;
        }

        const session = (await response.json()) as SessionView;
        if (run !== restoreRun.current) return;
        setMessages(session.messages);
        move(() => {
          setTasks(session.plan);
          setPending(session.pending_question);
        });

        const counts =
          `${count(session.messages.length, "message")}, ${count(session.plan.length, "task")}` +
          (session.pending_question ? ", 1 open question" : "");
        setNotice(
          sawOutage
            ? {
                tone: "ok",
                title: "Backend is back — session restored.",
                body: `It stopped answering and has returned. Everything shown was reloaded from its Postgres checkpoint: ${counts}.`,
              }
            : {
                tone: "ok",
                title: "Session restored from checkpoint.",
                body: `Rebuilt from Postgres: ${counts}. Nothing came from this browser.`,
              },
        );
        log("restored from checkpoint — earlier events were not replayed");
        return;
      }

      setConn("unreachable");
      setNotice({
        tone: "fail",
        title: "Backend unreachable.",
        body: "Your session is saved; nothing is lost.",
      });
    },
    [log],
  );

  // The id is kept in sessionStorage so a REFRESH returns to the same thread.
  // Held only in React state, a refresh minted a new id: the paused workflow was
  // still safe in Postgres but unreachable, and the question it was waiting on
  // vanished from the screen — the durability was real and the UI hid it.
  //
  // sessionStorage, not localStorage: it is per-tab, so a second tab is a second
  // conversation rather than two tabs racing turns onto one thread (the backend
  // rejects that with a 409).
  useEffect(() => {
    const stored = sessionStorage.getItem(SESSION_KEY);
    const id = stored ?? `ui-${crypto.randomUUID()}`;
    sessionStorage.setItem(SESSION_KEY, id);
    setSessionId(id);

    // `conn` stays "connecting" — and the composer stays blocked — until the
    // restore settles. Otherwise a message typed in the first half-second races
    // the restore, which then overwrites the transcript it was just added to.
    // A brand-new id has nothing to restore, so no request is made for it.
    if (stored) void restore(id);
    else setConn("ready");

    return () => {
      restoreRun.current++;
    };
  }, [restore]);

  const newSession = useCallback(() => {
    restoreRun.current++;
    const id = `ui-${crypto.randomUUID()}`;
    sessionStorage.setItem(SESSION_KEY, id);
    setSessionId(id);
    setMessages([]);
    setTasks([]);
    setActivity([]);
    setPending(null);
    setTraceId(null);
    setNotice(null);
    setDraft("");
    setConn("ready");
  }, []);

  const send = useCallback(
    async (text: string) => {
      // The id is set by an effect on mount, so it is empty for one frame.
      // Sending without it would start a graph run on an empty thread_id.
      if (!sessionId) return;

      // An empty text is the explicit skip ("I don't have this yet"); the
      // backend reads an empty answer as nothing supplied (api/chat.py). It is
      // shown as words so the transcript does not carry a blank bubble.
      const skipping = text === "";
      setMessages((prev) => [...prev, { role: "user", text: skipping ? "Skipped for now." : text }]);
      setDraft("");
      setBusy(true);
      // "sending", not "sent": at this point nothing has been observed, and a
      // log that says "sent" and then "not sent" one line later contradicts itself.
      log(
        skipping
          ? `skipping · resuming ${pending?.workflow} with nothing`
          : pending
            ? `sending answer · resuming ${pending.workflow}`
            : "sending · planning",
      );

      // Assistant text arrives as `token` fragments; accumulate locally and
      // replace with the authoritative `final` when it lands.
      let streamed = "";
      let streamingNode = "";

      // What this browser watched the graph do, in order. It rides ON the
      // assistant turn rather than beside it so it stays attached to the answer
      // it belongs to when the transcript scrolls.
      const steps: string[] = [];

      // `steps` must be carried on every rewrite: this replaces the whole
      // message object, so rebuilding it as `{role, text}` dropped the trail on
      // the first token of every turn.
      const pushAssistant = (value: string) => {
        setMessages((prev) => {
          const turn: ChatMessage = { role: "assistant", text: value, steps: [...steps] };
          const last = prev[prev.length - 1];
          return last?.role === "assistant" ? [...prev.slice(0, -1), turn] : [...prev, turn];
        });
      };

      let opened = false;
      // The backend ends every turn with `done`, or `error` if the graph raised.
      // A stream that closes with neither was cut — the process died or a hop
      // dropped it. Ignoring this is how a dead backend used to look like a
      // quiet one: the read loop ended normally and the rows spun forever.
      let terminal = false;
      let failure: unknown = null;

      try {
        await streamChat({ session_id: sessionId, message: text }, (event) => {
          const data = event.data;
          if (data.trace_id) setTraceId(String(data.trace_id));

          // The open question is cleared by the FIRST EVENT of the answering
          // turn — proof the backend took the answer — not at submit. Cleared at
          // submit, a send that failed left the workflow paused in Postgres with
          // no question on screen and no way to know an answer was still owed.
          if (!opened) {
            opened = true;
            // The held row goes back to Running as its answer is taken.
            move(() => setPending(null));
            setNotice(null);
          }

          switch (event.event) {
            case "plan": {
              const planned = data.tasks as Task[];
              // EXTEND, never replace. The event carries only the tasks the
              // planner added THIS turn (graph nodes return deltas — the
              // merge_tasks contract), so replacing the list made every earlier
              // row vanish on the second turn: exactly the "plan extends
              // across a workflow switch" moment the plan exists to show.
              move(() =>
                setTasks((prev) => [
                  ...prev.filter((old) => !planned.some((task) => task.id === old.id)),
                  ...planned,
                ]),
              );
              // The trail opens with the plan, and this is the first thing that
              // creates the assistant turn — so the operator sees the system
              // working before any token arrives, instead of a blank pane.
              steps.push(`Planned ${count(planned.length, "task")}`);
              pushAssistant(streamed);
              log(`plan · ${count(planned.length, "task")}`);
              break;
            }

            case "step":
              steps.push(String(data.label ?? ""));
              pushAssistant(streamed);
              break;

            case "task": {
              const task = data as unknown as Task;
              // Upsert: a task first appears in the plan event, then changes
              // status repeatedly. Appending would duplicate every row.
              move(() =>
                setTasks((prev) => {
                  const index = prev.findIndex((t) => t.id === task.id);
                  if (index === -1) return [...prev, task];
                  const next = [...prev];
                  next[index] = task;
                  return next;
                }),
              );
              log(`${task.id} ${task.workflow} → ${task.status}`);
              break;
            }

            case "token": {
              // A turn can stream from two nodes — a workflow writes its summary,
              // then `compose` narrates the rest. Restart the draft when the node
              // changes, or the two arrive glued together as one answer until
              // `final` replaces them.
              const node = String(data.node ?? "");
              if (node !== streamingNode) {
                streamingNode = node;
                streamed = "";
              }
              streamed += String(data.text ?? "");
              pushAssistant(streamed);
              break;
            }

            case "interrupt": {
              const question = data as unknown as PendingQuestion;
              move(() => setPending(question));
              // A paused turn gets no `token` and no `final`, so this turn stays
              // trail-only — on purpose: the trail ending in "needs your answer",
              // directly above the question card. The pausing node has no step
              // label of its own (its update fires on the resume turn), so this
              // is where the trail learns it stopped.
              steps.push("needs your answer");
              pushAssistant(streamed);
              log(`paused · ${question.workflow} needs input`);
              break;
            }

            case "final":
              streamed = String(data.response ?? "");
              pushAssistant(streamed);
              log("answer composed");
              break;

            // A graph failure is reported in the row and the log, and never
            // pushed into the transcript: the assistant did not say it, and the
            // old code overwrote partially streamed text with it.
            case "error": {
              terminal = true;
              const message = String(data.message ?? data.kind ?? "unknown error");
              log(`error · ${message}`);
              setNotice({ tone: "fail", title: "The run reported an error.", body: message });
              break;
            }

            case "done":
              terminal = true;
              break;
          }
        });
      } catch (error) {
        failure = error;
      }
      setBusy(false);

      if (failure instanceof SendError) {
        // No stream opened, so there is no turn to show. Take the optimistic
        // user turn back out and return the text; the question (if any) is still
        // on screen because nothing cleared it.
        setMessages((prev) => prev.slice(0, -1));
        setDraft(text);
        if (failure.status === 401) return toSignIn();
        if (failure.status === 409) {
          setNotice({
            tone: "fail",
            title: "This session already has a turn running.",
            body: "Wait for it to finish, then send again.",
          });
        } else if (failure.status === 0 || failure.status >= 502) {
          // "No answer", not "not sent": the request may have landed before the
          // connection died (see SendError). restore() then shows what the
          // checkpoint really holds, which is the only honest arbiter.
          log("no answer · backend unreachable");
          void restore(sessionId, "No answer from the backend — your message is back in the box.", true);
        } else {
          setNotice({
            tone: "fail",
            title: `Request failed (${failure.status}).`,
            body: "Nothing was changed.",
          });
        }
      } else if (failure || !terminal) {
        // Cut mid-run. Whatever was checkpointed is safe; whatever was only on
        // the wire is gone. Redraw from the checkpoint rather than trust a
        // half-streamed screen.
        log("stream lost");
        void restore(sessionId, "Connection lost mid-run.");
      }
    },
    [sessionId, pending, log, restore],
  );

  // First match wins, worst news first: a dead backend outranks an open question.
  // News gets a FILLED chip and calm states a grey one, so a filled chip in the
  // sidebar always means "look".
  const [glyph, tone, label] =
    conn === "unreachable"
      ? (["cross", "bg-fail text-surface", "Backend unreachable"] as const)
      : conn === "retrying"
        ? (["arc", TONE.hold, "Connection lost, retrying"] as const)
        : busy
          ? (["arc", TONE.busy, "Live, streaming"] as const)
          : pending
            ? (["pause", TONE.hold, "Waiting on you"] as const)
            : conn === "connecting"
              ? (["circle", TONE.quiet, "Connecting"] as const)
              : (["circle", TONE.quiet, "Ready"] as const);

  return (
    // Desktop is a fixed-height app whose panes scroll themselves. On a phone the
    // page scrolls instead: three nested scroll areas in 400px is unusable.
    <div className="flex min-h-dvh flex-col lg:grid lg:h-dvh lg:grid-cols-[15rem_minmax(0,1fr)]">
      {/* The sidebar holds only things that are real: what this browser has
          observed of the backend, the way to start over, which workflows this
          session has used, who is signed in. No links — there is one screen, and
          a nav item that leads nowhere is a lie in the UI. Below lg it is a top
          bar, and the legend is dropped (every task row prints its workflow). */}
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-line px-4 py-3 lg:flex-col lg:flex-nowrap lg:items-stretch lg:gap-y-5 lg:border-b-0 lg:border-r lg:py-5">
        <h1 className="mr-auto text-lg font-bold tracking-tight lg:mr-0">Control Tower</h1>

        <p role="status">
          <Chip glyph={glyph} tone={tone}>
            {label}
          </Chip>
        </p>

        {/* Disabled mid-turn: abandoning a thread while its stream is still
            writing would leave the old turn's events landing in the new one. */}
        <div>
          <button type="button" onClick={newSession} disabled={busy} className={SIDE_BUTTON}>
            New session
          </button>
        </div>

        <section aria-label="Workflows in this session" className="hidden lg:block">
          <h2 className="text-xs font-semibold text-ink-3">Workflows in this session</h2>
          <ul className="mt-2 space-y-1.5 text-sm">
            {WORKFLOWS.map((workflow) => (
              <li key={workflow} className="flex items-center gap-2">
                <span aria-hidden="true" className={`h-2 w-2 rounded-full ${WORKFLOW_DOT[workflow]}`} />
                <span className="capitalize">{workflow}</span>
                <span className="figures ml-auto text-ink-2">
                  {count(tasks.filter((task) => task.workflow === workflow).length, "task")}
                </span>
              </li>
            ))}
          </ul>
        </section>

        <div className="flex items-center gap-3 lg:mt-auto lg:flex-col lg:items-stretch lg:gap-2 lg:border-t lg:border-line lg:pt-4">
          <p className="hidden text-sm sm:block">
            <span className="text-ink-3">Signed in as </span>
            <span className="font-semibold">{operator}</span>
          </p>
          {/* A real form POST, not an onClick: sign-out is a state change on the
              server, and a GET could be triggered by any page embedding the URL
              (app/auth/sign-out/route.ts). */}
          <form action="/auth/sign-out" method="post">
            <button type="submit" className={SIDE_BUTTON}>
              Sign out
            </button>
          </form>
        </div>
      </header>

      <div className="flex min-w-0 flex-1 flex-col lg:min-h-0">
        {/* The live region is always mounted and only its content changes: a
            role="status" element inserted together with its text is not reliably
            announced. No close button — it clears on the next turn that starts. */}
        <div role="status">
          {notice && (
            <div
              className={`flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2.5 text-sm sm:px-6 ${NOTICE_TONE[notice.tone].style}`}
            >
              <StatusGlyph glyph={NOTICE_TONE[notice.tone].glyph} />
              <p className="min-w-0 flex-1 basis-64">
                <strong className="font-semibold">{notice.title}</strong> {notice.body}
              </p>
              {/* "Unreachable" always carries an action; a dead end would leave a
                  refresh as the only way forward. */}
              {conn === "unreachable" && (
                <button
                  type="button"
                  onClick={() => void restore(sessionId, undefined, true)}
                  className="rounded-lg border border-fail bg-surface px-3 py-1 font-semibold transition-colors duration-150 hover:bg-fail-wash"
                >
                  Retry now
                </button>
              )}
            </div>
          )}
        </div>

        <main className="flex flex-1 flex-col lg:grid lg:min-h-0 lg:grid-cols-[minmax(0,1fr)_25rem]">
          <ChatPanel
            messages={messages}
            pending={pending}
            busy={busy}
            connecting={conn !== "ready"}
            draft={draft}
            onDraft={setDraft}
            onSend={send}
            onSkip={() => void send("")}
          />
          <WorkflowPanel
            tasks={tasks}
            pending={pending}
            live={busy}
            activity={activity}
            sessionId={sessionId}
            traceId={traceId}
          />
        </main>
      </div>
    </div>
  );
}
