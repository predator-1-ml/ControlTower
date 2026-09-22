/**
 * The door: runs before every matched request (Next 16's name for middleware;
 * Node.js runtime, so `node:crypto` in lib/session.ts is available).
 *
 * It is the ONLY place the session is checked, and that is safe here for a
 * specific reason: everything worth protecting is a request that passes through
 * it. The pages hold no data — every byte of customer data arrives via /bff/*,
 * which is matched below. Next's own guidance is to re-check next to the data
 * when using Server Functions or server-rendered data; this app has neither.
 */
import { NextResponse, type NextRequest } from "next/server";
import { readSession, SESSION_COOKIE } from "@/lib/session";

export function proxy(req: NextRequest): NextResponse {
  const signedIn = readSession(req.cookies.get(SESSION_COOKIE)?.value) !== null;
  const { pathname } = req.nextUrl;

  if (pathname === "/sign-in") {
    // Already signed in: the form has nothing to offer.
    return signedIn ? NextResponse.redirect(new URL("/", req.url)) : NextResponse.next();
  }
  if (signedIn) return NextResponse.next();

  // An API caller gets a status it can act on. Redirecting it would hand
  // `fetch` the sign-in page's HTML with a 200, which the SSE reader would then
  // try to parse as an event stream.
  if (pathname.startsWith("/bff/")) {
    return NextResponse.json({ error: "signed_out" }, { status: 401 });
  }
  return NextResponse.redirect(new URL("/sign-in", req.url));
}

export const config = {
  // Everything except Next's own static assets and the sign-in POST target.
  // Without the exclusion the sign-in page's CSS and fonts would be redirected
  // to the sign-in page. /auth/* is excluded because it is how you GET a session.
  matcher: ["/((?!_next/static|_next/image|favicon.ico|auth/).*)"],
};
