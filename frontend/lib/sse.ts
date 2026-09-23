/**
 * Minimal SSE client for POST requests.
 *
 * The browser's built-in EventSource only does GET, and starting a turn needs a
 * request body. So this reads the response stream and parses the SSE framing by
 * hand — which is about twenty lines and avoids a dependency.
 *
 * Three details that matter, all learned the hard way:
 *
 * - **Line endings are CRLF.** sse-starlette (the server here) separates lines
 *   with "\r\n", so frames end with "\r\n\r\n". A parser looking for "\n\n"
 *   never matches, buffers the entire response, and emits nothing — while the
 *   network tab shows a perfectly healthy 200 with kilobytes transferred. The
 *   whole UI silently did nothing until this was found. The buffer is normalised
 *   to "\n" before any boundary search.
 *
 * - Frames are separated by a BLANK LINE, not by a newline. Splitting per line
 *   emits half-frames the moment a payload spans two network chunks, and
 *   JSON.parse on a half-frame throws and kills the stream.
 *
 * - The event name persists across a frame's lines, so `event:` must be
 *   remembered until its matching `data:` arrives.
 */

export type ServerEvent = {
  event: string;
  data: Record<string, unknown>;
};

/**
 * No stream opened: the fetch itself failed (`status` 0) or the answer was not a
 * 2xx stream.
 *
 * A type carrying the status, not a formatted string, because the page branches
 * on it: 0 and >= 502 mean "backend unreachable, start retrying", 409 means "a
 * turn is already running". The old `chat failed (502): {"error":...}` message
 * could only be printed, and it was — as an assistant chat turn, raw proxy JSON
 * included.
 *
 * Anything thrown from here that is NOT a SendError happened after the stream
 * opened, so the page knows a turn was running and must not offer the text back.
 *
 * What a SendError does NOT prove: that the backend never saw the message. With
 * status 0 or 502 the request can have been delivered and the connection cut
 * before any response byte came back — Node flushes headers with the first body
 * chunk, and the planner takes a second or two to produce one. Observed, not
 * theorised: the checkpoint held the HumanMessage while the browser saw a failed
 * fetch. Hence the page says "no answer", never "not sent", and redraws from the
 * checkpoint rather than from its own belief about what was delivered.
 */
export class SendError extends Error {
  constructor(readonly status: number) {
    super(`chat stream did not open (${status})`);
  }
}

export async function streamChat(
  body: { session_id: string; message: string },
  onEvent: (event: ServerEvent) => void,
): Promise<void> {
  // Relative URL, deliberately: this is the BFF proxy (ADR-001). An absolute
  // URL here would require a publicly reachable backend and destroy the
  // private-domain property the whole deployment is built around.
  const response = await fetch("/bff/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
  }).catch(() => null);

  if (!response?.ok || !response.body) throw new SendError(response?.status ?? 0);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // Normalise CRLF before searching for a boundary. Safe against a chunk that
    // splits "\r" from "\n": the lone "\r" simply stays in the buffer until its
    // "\n" arrives, and only complete frames are ever consumed.
    buffer = buffer.replace(/\r\n/g, "\n");

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
