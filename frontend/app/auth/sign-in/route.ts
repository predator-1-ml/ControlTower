/**
 * POST /auth/sign-in — the target of a plain HTML form.
 *
 * A native form post, answered with a redirect, rather than fetch + JSON. The
 * sign-in page therefore needs no JavaScript of its own, password managers see
 * the ordinary submit-then-navigate they are built to recognise, and there is no
 * client-side auth state to keep in sync with the cookie.
 *
 * Under /auth, not /bff: /bff/* is the proxy to the backend and is itself behind
 * the session check, so sign-in could never be reached there.
 */
import { checkCredentials, createSession, SESSION_COOKIE, SESSION_TTL_SECONDS } from "@/lib/session";

export const dynamic = "force-dynamic";

/**
 * 303, so the browser follows with a GET whatever method it arrived with. The
 * Location is RELATIVE on purpose: behind the ALB `req.url` carries the task's
 * private address, and an absolute redirect built from it would send the browser
 * to a host it cannot reach.
 */
function redirect(location: string, cookie?: string): Response {
  const headers = new Headers({ Location: location });
  if (cookie) headers.set("Set-Cookie", cookie);
  return new Response(null, { status: 303, headers });
}

export async function POST(req: Request): Promise<Response> {
  const form = await req.formData();
  const username = String(form.get("username") ?? "");
  const password = String(form.get("password") ?? "");

  if (!checkCredentials(username, password)) {
    // There is no lockout and no rate limit here — one process cannot count
    // attempts across tasks. A fixed delay at least makes guessing slow per
    // connection; the real control is a WAF rate rule on the ALB.
    await new Promise((resolve) => setTimeout(resolve, 600));
    // One message for both wrong username and wrong password: saying which one
    // was wrong tells an attacker which usernames exist.
    return redirect("/sign-in?error=credentials");
  }

  // `Secure` only when the request really arrived over TLS. The demo ALB is plain
  // HTTP (no domain to issue a certificate for), and a Secure cookie is silently
  // dropped by the browser on HTTP — sign-in would "succeed" and bounce straight
  // back to the form. With a certificate on the ALB this turns itself on.
  const secure = req.headers.get("x-forwarded-proto") === "https" ? "; Secure" : "";
  return redirect(
    "/",
    `${SESSION_COOKIE}=${createSession(username)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${SESSION_TTL_SECONDS}${secure}`,
  );
}
