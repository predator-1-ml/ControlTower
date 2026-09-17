/**
 * Minimal SSE client for POST requests.
 *
 * The browser's built-in EventSource only does GET, and starting a turn needs a
 * request body. So this reads the response stream and parses the SSE framing by
 * hand — which is about twenty lines and avoids a dependency.
 *
 * Two details that matter:
 *
 * - Frames are separated by a BLANK LINE, not by newline. A parser that splits
 *   on "\n" will emit half-frames the moment a payload is large enough to be
 *   split across chunks — and JSON.parse on a half-frame throws, killing the
 *   stream. The buffer below only consumes up to the last complete "\n\n".
 *
 * - The event name persists across the frame's lines, so `event:` must be
 *   remembered until its matching `data:` arrives.
 */

export type ServerEvent = {
  event: string;
  data: Record<string, unknown>;
};

export async function streamChat(
  body: { session_id: string; message: string },
  onEvent: (event: ServerEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  // Relative URL, deliberately: this is the BFF proxy (ADR-001). An absolute
  // URL here would require a publicly reachable backend and destroy the
  // private-domain property the whole deployment is built around.
  const response = await fetch("/bff/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok || !response.body) {
    const detail = await response.text().catch(() => "");
    throw new Error(`chat failed (${response.status}): ${detail.slice(0, 200)}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // Consume only whole frames; leave any partial tail in the buffer.
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const parsed = parseFrame(frame);
      if (parsed) onEvent(parsed);
      boundary = buffer.indexOf("\n\n");
    }
  }
}

function parseFrame(frame: string): ServerEvent | null {
  let event = "message";
  const dataLines: string[] = [];

  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    // ":" comments are sse-starlette's keep-alive pings. Ignoring them is what
    // keeps the connection alive without surfacing noise to the UI.
  }

  if (dataLines.length === 0) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return null;
  }
}
