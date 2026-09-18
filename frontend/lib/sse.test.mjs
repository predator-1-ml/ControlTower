/**
 * Parser tests for the SSE client.
 *
 * Run: node --test frontend/lib/sse.test.mjs
 *
 * These exist because the CRLF bug was INVISIBLE from the outside: the request
 * returned 200, the network tab showed kilobytes transferred, and the UI simply
 * rendered nothing. Nothing logged, nothing threw. The only signal was an empty
 * screen.
 *
 * The parser is duplicated here rather than imported because lib/sse.ts is
 * TypeScript and this runs under plain node --test. Keep the two in sync; the
 * logic is ~20 lines.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

function parseFrames(chunks) {
  const events = [];
  let buffer = "";

  for (const chunk of chunks) {
    buffer += chunk;
    buffer = buffer.replace(/\r\n/g, "\n");

    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);

      let event = "message";
      const dataLines = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length) {
        try {
          events.push({ event, data: JSON.parse(dataLines.join("\n")) });
        } catch {
          /* ignore malformed */
        }
      }
      boundary = buffer.indexOf("\n\n");
    }
  }
  return events;
}

test("parses CRLF frames — the shape sse-starlette actually sends", () => {
  // This is the regression. With "\n\n"-only boundary detection this returns [].
  const events = parseFrames(['event: plan\r\ndata: {"tasks":[]}\r\n\r\n']);
  assert.equal(events.length, 1);
  assert.equal(events[0].event, "plan");
});

test("parses LF frames too", () => {
  const events = parseFrames(['event: token\ndata: {"text":"hi"}\n\n']);
  assert.equal(events[0].data.text, "hi");
});

test("a frame split across network chunks is not emitted early", () => {
  const events = parseFrames(['event: final\r\ndata: {"resp', 'onse":"done"}\r\n\r\n']);
  assert.equal(events.length, 1, "half-frame must not be parsed");
  assert.equal(events[0].data.response, "done");
});

test("a chunk boundary between CR and LF is handled", () => {
  const events = parseFrames(['event: task\r\ndata: {"id":"t1"}\r', '\n\r\n']);
  assert.equal(events.length, 1);
  assert.equal(events[0].data.id, "t1");
});

test("multiple frames in one chunk all emit, in order", () => {
  const events = parseFrames([
    'event: plan\r\ndata: {"n":1}\r\n\r\nevent: task\r\ndata: {"n":2}\r\n\r\n',
  ]);
  assert.deepEqual(events.map((e) => e.data.n), [1, 2]);
});

test("keep-alive comments are ignored, not treated as events", () => {
  // sse-starlette sends ": ping" comments to hold the connection open.
  const events = parseFrames([': ping\r\n\r\nevent: token\r\ndata: {"text":"x"}\r\n\r\n']);
  assert.equal(events.length, 1);
  assert.equal(events[0].event, "token");
});

test("a malformed frame is skipped without killing the stream", () => {
  const events = parseFrames([
    "event: bad\r\ndata: {not json}\r\n\r\n",
    'event: token\r\ndata: {"text":"survived"}\r\n\r\n',
  ]);
  assert.equal(events.length, 1);
  assert.equal(events[0].data.text, "survived");
});
