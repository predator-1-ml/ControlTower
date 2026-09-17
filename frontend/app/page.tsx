"use client";

import { useCallback, useRef, useState } from "react";

import { ChatPanel } from "@/components/ChatPanel";
import { WorkflowPanel } from "@/components/WorkflowPanel";
import { streamChat } from "@/lib/sse";
import type { ChatMessage, PendingQuestion, Task } from "@/lib/types";

/**
 * Single operational screen: conversation on the left, plan and activity on the
 * right. State lives here because both panes are driven by the same event
 * stream, and threading it through a store would add indirection without
 * removing any coupling.
 */
export default function Page() {
  // Stable for the tab's lifetime. This is the checkpointer's thread_id, so it
  // is also what makes a workflow resumable — including after the backend has
  // been restarted underneath it.
  const sessionId = useRef(`ui-${crypto.randomUUID()}`).current;

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [activity, setActivity] = useState<string[]>([]);
  const [pending, setPending] = useState<PendingQuestion | null>(null);
  const [traceId, setTraceId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const send = useCallback(
    async (text: string) => {
      setMessages((prev) => [...prev, { role: "user", text }]);
      setBusy(true);
      setPending(null);

      // Assistant text arrives as `token` fragments; accumulate locally and
      // replace with the authoritative `final` when it lands.
      let streamed = "";
      const pushAssistant = (value: string) => {
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (last?.role === "assistant") {
            return [...prev.slice(0, -1), { role: "assistant", text: value }];
          }
          return [...prev, { role: "assistant", text: value }];
        });
      };

      try {
        await streamChat({ session_id: sessionId, message: text }, (event) => {
          const data = event.data as Record<string, never>;
          if (data.trace_id) setTraceId(String(data.trace_id));

          switch (event.event) {
            case "plan":
              setTasks(data.tasks as unknown as Task[]);
              setActivity((prev) => [...prev, "planned"]);
              break;

            case "task": {
              const task = data as unknown as Task;
              // Upsert: a task first appears in the plan event, then changes
              // status repeatedly. Appending would duplicate every row.
              setTasks((prev) => {
                const index = prev.findIndex((t) => t.id === task.id);
                if (index === -1) return [...prev, task];
                const next = [...prev];
                next[index] = task;
                return next;
              });
              setActivity((prev) => [...prev, `${task.id} ${task.action} → ${task.status}`]);
              break;
            }

            case "token":
              streamed += String(data.text ?? "");
              pushAssistant(streamed);
              break;

            case "progress":
              setActivity((prev) => [...prev, JSON.stringify(data)]);
              break;

            case "interrupt":
              setPending(data as unknown as PendingQuestion);
              setActivity((prev) => [...prev, "paused — needs input"]);
              break;

            case "final":
              streamed = String(data.response ?? "");
              pushAssistant(streamed);
              break;

            case "error":
              setActivity((prev) => [...prev, `error: ${data.message ?? data.kind}`]);
              pushAssistant(
                streamed || `Something failed: ${data.message ?? data.kind ?? "unknown"}`,
              );
              break;
          }
        });
      } catch (error) {
        pushAssistant(`Request failed: ${(error as Error).message}`);
      } finally {
        setBusy(false);
      }
    },
    [sessionId],
  );

  return (
    <main className="mx-auto grid h-screen max-w-6xl grid-cols-1 gap-6 p-6 lg:grid-cols-[1fr_360px]">
      <section className="flex min-h-0 flex-col">
        <h1 className="mb-1 text-lg font-semibold text-slate-800">AI Operations Control Tower</h1>
        <p className="mb-4 text-sm text-slate-500">
          Onboarding, claims and knowledge — one conversation.
        </p>
        <div className="min-h-0 flex-1">
          <ChatPanel messages={messages} pending={pending} busy={busy} onSend={send} />
        </div>
      </section>

      <div className="min-h-0">
        <WorkflowPanel
          tasks={tasks}
          activity={activity}
          sessionId={sessionId}
          traceId={traceId}
        />
      </div>
    </main>
  );
}
