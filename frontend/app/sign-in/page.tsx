import type { Metadata } from "next";

export const metadata: Metadata = { title: "Sign in · Control Tower" };

// Label above, input below, error under the form: the order every form library
// and every password manager expects. No placeholder-as-label — it vanishes the
// moment the operator types and leaves a filled box with no name.
const LABEL = "block text-sm font-semibold";
const FIELD = "mt-1.5 w-full rounded-lg border border-line-strong bg-surface px-3 py-2.5";

/**
 * The door.
 *
 * A SERVER component with a plain HTML form: it ships no JavaScript of its own,
 * works before hydration, and gives password managers the ordinary
 * submit-then-navigate they recognise. The credential check is in
 * app/auth/sign-in/route.ts; a failure comes back as `?error=credentials`.
 *
 * There is no sign-up and no "forgot password" because there is no user store —
 * one operator credential comes from the environment (lib/session.ts). A link
 * that leads nowhere would be a lie in the UI.
 *
 * Design: one card on the same grey frame as the app's sidebar, in the same
 * card the plan sits in inside — the door and the room are one system. It is the
 * sign-in page every internal tool has, on purpose: nobody should have to think
 * here. The button is the accent because signing in is the operator's act.
 * Rejected: a split page with a marketing panel — there are no customers,
 * metrics or claims to put in it (PRODUCT.md), and filler would be invention.
 */
export default async function SignInPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const failed = (await searchParams).error === "credentials";

  return (
    <main className="flex min-h-dvh items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm">
        <h1 className="text-2xl font-bold tracking-tight">Control Tower</h1>
        <p className="mt-2 text-ink-2">
          One conversation over onboarding, claims and policy. You see the plan before anything
          runs, and it shows when it is waiting on you.
        </p>

        <form
          action="/auth/sign-in"
          method="post"
          className="mt-6 space-y-4 rounded-lg border border-line bg-surface p-6 shadow-card"
        >
          <div>
            <label htmlFor="username" className={LABEL}>
              Operator
            </label>
            <input
              id="username"
              name="username"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              required
              autoFocus
              className={FIELD}
            />
          </div>

          <div>
            <label htmlFor="password" className={LABEL}>
              Password
            </label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              required
              className={FIELD}
            />
          </div>

          <button
            type="submit"
            className="w-full rounded-lg bg-accent px-5 py-2.5 font-semibold text-surface transition-colors duration-150 hover:bg-accent-deep active:translate-y-px"
          >
            Sign in
          </button>

          {/* One message for a wrong username OR password: saying which would
              confirm which usernames exist. `empty:hidden` so the form has no
              blank gap when there is nothing to say; the page is server-rendered
              per request, so the text never changes after load. */}
          <p role="alert" className="text-sm font-medium text-fail empty:hidden">
            {failed && "Those details do not match an operator account. Check them and try again."}
          </p>
        </form>

        <p className="mt-4 text-sm text-ink-2">
          Accounts are provisioned by an administrator; there is no sign-up. A session lasts eight
          hours.
        </p>
      </div>
    </main>
  );
}
