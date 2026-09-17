/**
 * BFF proxy — the mechanism behind ADR-001.
 *
 * Browser ──▶ public ALB ──▶ THIS HANDLER ──▶ http://api.internal:8000 ──▶ backend
 *
 * Why this file exists at all:
 *
 * The assignment requires frontend→backend traffic to cross a PRIVATE domain
 * name. A browser cannot resolve a private VPC name, so the usual
 * `NEXT_PUBLIC_API_URL` approach silently requires a publicly-reachable backend
 * — which does not satisfy the requirement. Routing the call through a server
 * component instead means every frontend→backend byte stays inside the VPC, and
 * the backend can sit with zero public ingress.
 *
 * A second benefit that is easy to miss: client components call `/bff/...`, a
 * RELATIVE url. There is no public API origin to bake into the bundle, so one
 * image promotes unchanged from local to dev to prod. `NEXT_PUBLIC_*` is
 * substituted by the bundler at `next build`, not read at runtime — so a
 * build-time-pinned backend URL is exactly the trap this design avoids.
 *
 * ⚠️ Do NOT reimplement this with `next.config.js` rewrites(). Rewrites are
 * evaluated at build time and frozen into routes-manifest.json; `next start`
 * never re-reads them, so `destination: process.env.BACKEND_INTERNAL_URL`
 * reproduces the build-time problem somewhere nobody thinks to look. Rewrites
 * have also historically dropped text/event-stream bodies.
 *
 * ⚠️ Mounted at /bff, not /api, so that an ALB rule for /api/* can never shadow
 * these handlers.
 */

// Never prerender or cache. Also forces the patched fetch onto the dynamic path.
export const dynamic = "force-dynamic";
// runtime defaults to 'nodejs', which is required — the Edge runtime cannot
// reach a VPC-private host (and is deprecated as of Next 16.3 anyway).

const BACKEND = process.env.BACKEND_INTERNAL_URL ?? "http://localhost:8000";

/**
 * Headers we are willing to forward downstream.
 *
 * This is an ALLOWLIST, and that is load-bearing rather than fussy. Undici
 * auto-requests gzip, transparently decompresses the body, and then leaves
 * `content-encoding: gzip` AND the original `content-length` on the headers
 * object. Spreading those onto an already-decoded stream gives the browser
 * either ERR_CONTENT_DECODING_FAILED or a silent truncation at the bogus
 * length — with nothing logged server-side.
 */
const SAFE_RESPONSE_HEADERS = ["content-type", "cache-control", "x-trace-id"];

function buildResponseHeaders(upstream: Response, isStream: boolean): Headers {
  const headers = new Headers();
  for (const name of SAFE_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  if (isStream) {
    headers.set("Content-Type", "text/event-stream; charset=utf-8");
    // `no-transform` is the documented opt-out from Next's own gzip, which does
    // compress text/event-stream. It survives today only because Next flushes
    // per chunk — but never put nginx or Express compression in front of this,
    // or you lose the flush and the stream batches.
    headers.set("Cache-Control", "no-cache, no-store, no-transform, must-revalidate");
    // nginx-specific; the ALB ignores it. Kept as insurance for any other hop.
    headers.set("X-Accel-Buffering", "no");
    headers.set("Connection", "keep-alive");
  }
  return headers;
}

async function proxy(req: Request, path: string[]): Promise<Response> {
  const suffix = path.join("/");
  const search = new URL(req.url).search;
  const target = `${BACKEND}/${suffix}${search}`;

  const wantsStream = (req.headers.get("accept") ?? "").includes("text/event-stream");

  const headers: HeadersInit = {
    // Stop undici negotiating compression at all — removes the stale-header
    // ambiguity at the source rather than papering over it downstream.
    "Accept-Encoding": "identity",
  };
  const contentType = req.headers.get("content-type");
  if (contentType) headers["Content-Type"] = contentType;
  if (wantsStream) headers["Accept"] = "text/event-stream";

  const hasBody = req.method !== "GET" && req.method !== "HEAD";

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: req.method,
      headers,
      body: hasBody ? await req.text() : undefined,
      // Bypass Next's patched-fetch cache path, which can tee or buffer the
      // whole body and collapse a stream into a single chunk.
      cache: "no-store",
      // Propagate client disconnect upstream so an abandoned stream stops
      // burning backend work (and LLM tokens) instead of running to completion.
      signal: req.signal,
    });
  } catch (err) {
    return Response.json(
      { error: "backend_unreachable", detail: String(err) },
      { status: 502 },
    );
  }

  if (!upstream.body) {
    return new Response(null, {
      status: upstream.status,
      headers: buildResponseHeaders(upstream, false),
    });
  }

  // Pass the ReadableStream straight through. Never `await upstream.text()` or
  // `for await` it here — that buffers the entire response and silently turns a
  // stream into a single blob delivered at the end.
  return new Response(upstream.body, {
    status: upstream.status,
    headers: buildResponseHeaders(upstream, wantsStream),
  });
}

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(req: Request, ctx: Ctx) {
  return proxy(req, (await ctx.params).path);
}

export async function POST(req: Request, ctx: Ctx) {
  return proxy(req, (await ctx.params).path);
}

export async function DELETE(req: Request, ctx: Ctx) {
  return proxy(req, (await ctx.params).path);
}
