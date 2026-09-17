"use client";

import { useRef, useState } from "react";

import type { ChatMessage, PendingQuestion } from "@/lib/types";

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
  onSend,
}: {
  messages: ChatMessage[];
  pending: PendingQuestion | null;
  busy: boolean;
  onSend: (text: string) => void;
}) {
  const [draft, setDraft] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    onSend(text);
    setDraft("");
    requestAnimationFrame(() => endRef.current?.scrollIntoView({ behavior: "smooth" }));
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 space-y-3 overflow-y-auto pr-1">
        {messages.length === 0 && (
          <div className="rounded-lg bg-slate-50 p-4 text-sm text-slate-500">
            <p className="mb-2 font-medium text-slate-600">Try:</p>
            <ul className="space-y-1">
              <li>&ldquo;Onboard CUST-1002 and check if they have an active claim&rdquo;</li>
              <li>&ldquo;Summarise claim CLM-5003&rdquo;</li>
              <li>&ldquo;When does a claim need a second review?&rdquo;</li>
            </ul>
          </div>
        )}

        {messages.map((message, index) => (
          <div
            key={index}
            className={
              message.role === "user"
                ? "ml-auto max-w-[85%] rounded-lg bg-slate-800 px-3 py-2 text-sm text-white"
                : "mr-auto max-w-[85%] whitespace-pre-wrap rounded-lg bg-white px-3 py-2 text-sm shadow-sm ring-1 ring-slate-200"
            }
          >
            {message.text}
          </div>
        ))}
        <div ref={endRef} />
      </div>

      {pending && (
        <div className="mt-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          <p className="font-medium">Waiting on you</p>
          <p className="mt-0.5">{pending.question}</p>
        </div>
      )}

      <form onSubmit={submit} className="mt-3 flex gap-2">
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={pending ? "Answer the question above…" : "Ask the control tower…"}
          disabled={busy}
          className="flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500 disabled:bg-slate-100"
        />
        <button
          type="submit"
          disabled={busy || draft.trim() === ""}
          className="rounded-md bg-slate-800 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
        >
          {busy ? "Working…" : "Send"}
        </button>
      </form>
    </div>
  );
}
