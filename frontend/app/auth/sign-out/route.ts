/**
 * POST /auth/sign-out — expires the cookie and returns to the sign-in page.
 *
 * POST, not GET: a GET sign-out can be triggered by any page that embeds
 * `<img src="/auth/sign-out">`. SameSite=Lax does not send the cookie on a
 * cross-site POST, so this one cannot be forced from elsewhere.
 *
 * There is nothing to revoke server-side — the session is only the signed
 * cookie — which is the accepted cost of having no session store: a copied cookie
 * stays valid until its 8-hour expiry.
 */
import { SESSION_COOKIE } from "@/lib/session";

export const dynamic = "force-dynamic";

export async function POST(): Promise<Response> {
  return new Response(null, {
    status: 303,
    headers: {
      Location: "/sign-in",
      "Set-Cookie": `${SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0`,
    },
  });
}
