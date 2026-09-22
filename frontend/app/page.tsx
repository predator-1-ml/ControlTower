import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { Workspace } from "@/components/Workspace";
import { readSession, SESSION_COOKIE } from "@/lib/session";

/**
 * A server component, so the operator's name can be read from the httpOnly
 * cookie — which the browser's own JavaScript cannot see, by design.
 *
 * proxy.ts has already turned away anyone without a session; the check is
 * repeated here because this is where the name is actually USED, and a render
 * that trusts a guard in another file is one matcher typo away from showing the
 * workspace to nobody-in-particular.
 */
export default async function Page() {
  const operator = readSession((await cookies()).get(SESSION_COOKIE)?.value);
  if (!operator) redirect("/sign-in");
  return <Workspace operator={operator} />;
}
