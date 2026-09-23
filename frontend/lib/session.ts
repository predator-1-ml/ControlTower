/**
 * Operator session: a signed cookie, checked on the server. No user store.
 *
 * Why auth exists at all: the tool is for internal operators, but the load
 * balancer in front of it is public. Without this, anyone with the URL can drive
 * the workflows and spend Bedrock tokens.
 *
 * Why it is this small: the brief does not ask for identity management, and every
 * line here has to be explained out loud. One operator credential comes from the
 * environment; a successful sign-in is remembered in a cookie that the server
 * can verify without storing anything.
 *
 * The cookie is `username.expiry.signature`, where signature is an HMAC-SHA256
 * over `username.expiry` keyed by SESSION_SECRET. The browser can read neither
 * (httpOnly) nor forge it (no key).
 *
 * Rejected:
 *  - NextAuth / Auth.js — a dependency, a database adapter and a provider config
 *    to defend, for one static credential.
 *  - A JWT library — the same HMAC, plus a header and an `alg` field whose whole
 *    history is "alg: none" attacks. Three dot-separated fields need no parser.
 *  - Checking the password in the browser — the credential would ship in the JS
 *    bundle and `/bff/*` would stay open to anyone with curl.
 */
import { createHmac, timingSafeEqual } from "node:crypto";

export const SESSION_COOKIE = "ct_session";

// One working shift. There is no refresh: an operator signs in again tomorrow.
export const SESSION_TTL_SECONDS = 8 * 60 * 60;

/**
 * Compare without leaking, through timing, how many leading characters matched.
 * timingSafeEqual throws on unequal lengths, so both sides are hashed to a fixed
 * 32 bytes first — which also hides the length of the real value.
 */
function safeEqual(a: string, b: string): boolean {
  const digest = (s: string) => createHmac("sha256", "compare").update(s).digest();
  return timingSafeEqual(digest(a), digest(b));
}

function sign(payload: string, secret: string): string {
  return createHmac("sha256", secret).update(payload).digest("base64url");
}

/**
 * FAILS CLOSED: with any of the three variables unset, nobody can sign in. The
 * alternative — a default password in code — is a credential in a public repo.
 */
export function checkCredentials(username: string, password: string): boolean {
  const { OPERATOR_USERNAME, OPERATOR_PASSWORD, SESSION_SECRET } = process.env;
  if (!OPERATOR_USERNAME || !OPERATOR_PASSWORD || !SESSION_SECRET) return false;
  // Evaluate both so a wrong username and a wrong password take the same time.
  const userOk = safeEqual(username, OPERATOR_USERNAME);
  const passOk = safeEqual(password, OPERATOR_PASSWORD);
  return userOk && passOk;
}

export function createSession(username: string): string {
  const expiry = Math.floor(Date.now() / 1000) + SESSION_TTL_SECONDS;
  const payload = `${username}.${expiry}`;
  return `${payload}.${sign(payload, process.env.SESSION_SECRET!)}`;
}

/** The operator's username if the cookie is authentic and unexpired, else null. */
export function readSession(cookie: string | undefined): string | null {
  const secret = process.env.SESSION_SECRET;
  if (!cookie || !secret) return null;

  // Split from the RIGHT: a username may contain dots, the other two cannot.
  const lastDot = cookie.lastIndexOf(".");
  const payload = cookie.slice(0, lastDot);
  const signature = cookie.slice(lastDot + 1);
  const expiryDot = payload.lastIndexOf(".");
  if (lastDot < 0 || expiryDot < 0) return null;

  // Signature BEFORE expiry: never act on a field that has not been authenticated.
  if (!safeEqual(signature, sign(payload, secret))) return null;
  if (Number(payload.slice(expiryDot + 1)) < Date.now() / 1000) return null;
  return payload.slice(0, expiryDot);
}
