"use client";

import { useEffect, useRef } from "react";

import { StatusGlyph } from "@/components/Status";
import type { ChatMessage, PendingQuestion } from "@/lib/types";

// Copied verbatim from docs/demo.md so the screen and the demo script cannot
// disagree. Each opens a different branch; the hint says which, so the presenter
// can pick a beat without remembering customer ids.
const PROMPTS = [
  ["Onboard CUST-1001 and check whether they already have an active claim.", "plans two tasks"],
  ["Summarise claim CLM-5003.", "pauses for missing details"],
  ["When does a motor claim need a second review?", "knowledge, with citations"],
  ["Onboard CUST-1002", "pauses for documents — the crash-resume beat"],
] as const;

// ONE column for the whole pane: the heading, every turn, the question and the
// composer share its two edges. Without it the operator's bubbles hugged the
// pane's far right edge while answers stopped at their own measure on the left,
// so on a wide monitor the two speakers were not in the same conversation.
// 48rem keeps an answer near a 75-character line.
const COLUMN = "mx-auto w-full max-w-3xl";

// Two things a handler scans an answer for: the record ids they already know
// (CUST-1001, CLM-5003 — PRODUCT.md) and where a statement came from
// (`[claims-handling-policy.md, Escalation]`, written inline by the knowledge
// workflow from the chunks it actually retrieved).
//
// ONE capture group wrapping BOTH alternatives, not one per alternative: split()
// keeps every group, so a second group would put `undefined` at every other odd
// index and break the `index % 2` trick below.
//
// Parsed from the text rather than carried as a payload on `final`, because
// restored messages are `{role, text}` — a sources payload would vanish on
// refresh, and the citation would stop being a chip exactly when the operator
// came back to re-read it.
const MARKED = /(\b(?:CUST|CLM)-\d+\b|\[[\w-]+\.md, [^\]]+\])/;

function withRecordIds(text: string) {
  return text.split(MARKED).map((part, index) =>
    index % 2 === 1 ? (
      // nowrap: an id broken across two lines ("CLM-" / "5001") cannot be scanned.
      <span key={index} className="whitespace-nowrap rounded bg-sunken px-0.5 font-semibold">
        {part}
      </span>
    ) : (
      part
    ),
  );
}

/**
 * Conversation pane.
 *
 * When the graph is paused on an `interrupt()`, the composer changes to make the
 * pending question obvious. The message the user then sends is delivered as the
 * *answer* — the backend resumes rather than re-plans (see app/api/chat.py).
 * Showing an ordinary composer in that state would invite a new request and
 * silently discard the workflow waiting on them.
 */
export function ChatPanel({
  messages,
  pending,
  busy,
  connecting,
  draft,
  onDraft,
  onSend,
}: {
  messages: ChatMessage[];
  pending: PendingQuestion | null;
  /** A turn is open. */
  busy: boolean;
  /** No session id yet, or the backend has not been reached. */
  connecting: boolean;
  draft: string;
  onDraft: (text: string) => void;
  onSend: (text: string) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const endRef = useRef<HTMLDivElement>(null);

  // Follow the conversation on every change, not only on submit: streamed
  // answers and a question restored after a refresh both used to land below the
  // fold. Skipped while empty so a phone does not jump past the plan on load.
  useEffect(() => {
    if (messages.length === 0 && !pending) return;
    endRef.current?.scrollIntoView({ block: "nearest" });
  }, [messages, pending]);

  // On mount, and again whenever a question opens: the next thing the operator
  // does is type, so the caret should already be there. `preventScroll` because
  // focus() otherwise scrolls a phone to the composer, past the plan.
  useEffect(() => {
    inputRef.current?.focus({ preventScroll: true });
  }, [pending]);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || busy || connecting) return;
    onSend(text);
  }

  return (
    <section
      aria-label="Conversation"
      // order-2 on a phone only (plan first — see WorkflowPanel). `order` also
      // drives grid auto-placement, so left on at lg it swaps the two columns.
      // The one white column between two grey ones: this is where work happens.
      className="order-2 flex flex-1 flex-col bg-surface lg:order-none lg:min-h-0"
    >
      {/* Outside the scroll area, so the pane keeps its name while the
          transcript scrolls under it. lg:py-5 + leading-7 is the sidebar
          wordmark's box, so the two titles share a baseline across the hairline
          between them. */}
      <div className="border-b border-line px-4 py-3 sm:px-8 lg:py-5">
        <h2 className={`font-semibold leading-7 ${COLUMN}`}>Conversation</h2>
      </div>
      <div className="flex-1 px-4 py-4 sm:px-8 lg:min-h-0 lg:overflow-y-auto lg:py-6">
        <div className={`space-y-6 ${COLUMN}`}>
          {messages.length === 0 && !pending && (
            <div>
              <p className="text-2xl font-semibold tracking-tight">Start with a request.</p>
              <p className="mt-1 text-ink-2">
                Say what you need in plain words. The plan appears on the right before anything runs.
              </p>
              {/* -mx-2 here + px-2 on the buttons: the hover band gets breathing
                  room while the prompt text stays on the heading's left edge. */}
              <ul className="-mx-2 mt-6 border-b border-line">
                {PROMPTS.map(([prompt, hint]) => (
                  <li key={prompt} className="border-t border-line">
                    {/* Fills the composer; does NOT send. The operator stays in
                        control, and a presenter can narrate before pressing Enter. */}
                    <button
                      type="button"
                      onClick={() => {
                        onDraft(prompt);
                        inputRef.current?.focus();
                      }}
                      className="w-full rounded-lg px-2 py-2.5 text-left transition-colors duration-150 hover:bg-sunken"
                    >
                      {/* Accent: it is pressable, and it is what the OPERATOR
                          would write (globals.css, rule 2). */}
                      <span className="block font-medium text-accent">{prompt}</span>
                      <span className="block text-sm text-ink-2">{hint}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* aria-busy while a turn streams: a screen reader then announces the
              finished answer once instead of every token fragment. */}
          <div role="log" aria-live="polite" aria-busy={busy} className="space-y-6">
            {messages.map((message, index) =>
              message.role === "user" ? (
                // Accent on its wash: the operator's own words. The speaker is also
                // named for a screen reader, which cannot see colour.
                <p
                  key={index}
                  className="ml-auto w-fit max-w-[85%] whitespace-pre-wrap rounded-lg bg-accent-wash px-4 py-2.5 font-medium text-accent-deep"
                >
                  <span className="sr-only">You: </span>
                  {message.text}
                </p>
              ) : (
                // Named and badged, not boxed: this is a work log, not a messenger,
                // and one boxed speaker is enough to tell the two apart. The badge
                // gives every answer the same left edge to start from, which is
                // what makes a long transcript scannable.
                <div key={index} className="flex gap-3">
                  <span
                    aria-hidden="true"
                    className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-ink text-xs font-bold text-surface"
                  >
                    CT
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-semibold leading-8">Control Tower</p>
                    {/* The step trail: what this browser watched the graph do,
                        quiet and above the answer. Process belongs here, not in
                        the prose — the answer below is only the answer. One
                        wrapping line of plain text, not a component: it is a
                        sentence, and a row of pills would compete with the
                        question card for attention. */}
                    {message.steps && message.steps.length > 0 && (
                      <p className="mb-1.5 flex flex-wrap items-center gap-x-1.5 text-sm text-ink-2">
                        {busy && index === messages.length - 1 && (
                          <StatusGlyph glyph="arc" className="h-3.5 w-3.5" />
                        )}
                        {message.steps.join(" › ")}
                      </p>
                    )}
                    {/* `printing` adds the caret to the turn that is still growing.
                        break-words: a citation or a long reference otherwise
                        overflows a phone.

                        Only rendered once there is text: a turn that PAUSED gets
                        no token and no final, so it is a trail-only turn and an
                        empty paragraph would leave a blank line hanging under it. */}
                    {message.text && (
                      <p
                        className={`whitespace-pre-wrap break-words leading-relaxed ${
                          busy && index === messages.length - 1 ? "printing" : ""
                        }`}
                      >
                        {withRecordIds(message.text)}
                      </p>
                    )}
                  </div>
                </div>
              ),
            )}
          </div>

          {pending && (
            // role="alert", mounted together with its text — unlike the notice in
            // Workspace, which is role="status" and must pre-exist to be announced.
            // An inserted alert IS announced; that is what the role is for.
            // This is the one thing on screen that blocks progress,
            // so it is also the LARGEST thing in the conversation — scale follows
            // urgency. Full border plus wash — not a thick coloured left edge,
            // which is the stock "callout" look and says nothing a border does not.
            <div
              role="alert"
              className="flex gap-3 rounded-lg border border-hold bg-hold-wash p-4 text-hold"
            >
              <StatusGlyph glyph="pause" className="mt-1.5 h-5 w-5" />
              <div className="min-w-0 space-y-2">
                <p className="text-xl font-semibold tracking-tight">
                  <span className="capitalize">{pending.workflow}</span> needs an answer from you
                </p>
                <p className="text-lg text-ink">{pending.question}</p>
                {/* Shown as text, not as one input per field: POST /chat takes a
                    single string and delivers it as Command(resume=...). A form
                    would promise structure the backend never reads. `?.` because
                    the payload is whatever a node passed to interrupt(). */}
                {pending.fields?.length > 0 && (
                  <p className="text-sm">
                    Needed: {pending.fields.map((field) => field.replaceAll("_", " ")).join(" · ")}
                  </p>
                )}
                <p className="text-sm">
                  Your reply resumes the workflow from this step. The question is saved in the
                  checkpoint, so it survives a refresh or a backend restart.
                </p>
              </div>
            </div>
          )}

          {/* scroll-mb: on a phone the sticky composer covers the bottom of the
              viewport, and "in view" must mean "above the composer". */}
          <div ref={endRef} className="scroll-mb-24" />
        </div>
      </div>

      <form
        onSubmit={submit}
        className="sticky bottom-0 border-t border-line bg-surface px-4 py-4 sm:px-8"
      >
        <div className={`flex gap-2 ${COLUMN}`}>
          <label htmlFor="composer" className="sr-only">
            {pending ? `Answer to resume ${pending.workflow}` : "Message"}
          </label>
          {/* Never disabled: disabling drops focus, so every turn would cost a
              click to get the caret back. Only submitting is blocked. */}
          <input
            id="composer"
            ref={inputRef}
            value={draft}
            onChange={(event) => onDraft(event.target.value)}
            autoComplete="off"
            placeholder={
              connecting
                ? "Connecting…"
                : pending
                  ? `Answer to resume ${pending.workflow}…`
                  : "Write a request…"
            }
            // The border is always 1px and the hold state adds a ring, so the box
            // does not change size (and nudge the button) when a question opens.
            className={`min-w-0 flex-1 rounded-lg border bg-surface px-3 py-2.5 placeholder:text-ink-3 ${
              pending ? "border-hold ring-1 ring-hold" : "border-line-strong"
            }`}
          />
          {/* The one accent-filled control on the screen: the primary action.
              While a question is open it takes the hold colour instead: the next
              send is an ANSWER and cannot be anything else (docs/tradeoffs.md).
            Disabled is its own grey pair (4.9:1), not opacity: a faded "Send
            answer" is unreadable in a compressed recording. */}
          <button
            type="submit"
            disabled={busy || connecting || draft.trim() === ""}
            className={`rounded-lg px-5 py-2.5 font-semibold text-surface transition-colors duration-150 active:translate-y-px disabled:bg-sunken disabled:text-ink-3 ${
              pending ? "bg-hold hover:bg-hold-deep" : "bg-accent hover:bg-accent-deep"
            }`}
          >
            {busy ? "Running…" : pending ? "Send answer" : "Send"}
          </button>
        </div>
      </form>
    </section>
  );
}
