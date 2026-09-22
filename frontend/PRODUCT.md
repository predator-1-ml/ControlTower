# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

**Primary: an internal insurance operations handler**, at a desk in a lit office, on
a laptop or desktop monitor, several hours a day. They take a customer request that
cuts across onboarding, claims and policy lookup, hand it to the system in plain
language, and need to know three things at all times: what the system decided to do,
what it is doing now, and whether it is waiting on them.

**Secondary: an interviewer watching a 3–5 minute recorded demo** (`docs/demo.md`).
They will not read code during the recording; a backend property that is true but
not visible on screen scores as absent.

Not customers. There is no public audience and no self-service sign-up — accounts are
provisioned, which is why there is a sign-in page and no sign-up page.

## Product Purpose

Control Tower is one conversational interface over three operational workflows
(customer onboarding, claims, policy knowledge). The handler types a goal; a planner
turns it into a task plan *before* anything runs; a supervisor routes each task to a
workflow; workflows pause for a human when a decision needs one; and the whole session
survives the backend process dying.

Success is a handler finishing a multi-workflow request without losing context, and
always knowing whose turn it is — theirs or the system's.

## Positioning

The plan is the product, not the chat. A chatbot shows you an answer; Control Tower
shows you the work: the plan it made, the order it ran in, where it stopped to ask a
person, and that it picked up exactly there after a restart. The state lives in
Postgres, not in the process or the browser.

## Operating Context

- Four moments carry the demo and the daily use alike: **plan appears before
  execution**; **the plan extends when the user switches workflow** and earlier
  results stay put; **a workflow pauses and asks the human**; **the backend dies,
  returns, and the session is rebuilt from the checkpoint** with the question still
  open.
- Requests reference real identifiers the handler already knows: customers
  (`CUST-1001`), claims (`CLM-5003`), task ids (`t1`), a trace id per turn.
- The knowledge workflow answers from internal policy documents and cites
  `[source, section]`.
- While a workflow is paused, the next message IS the answer to its question — the
  interface must make that unmistakable, because it cannot be undone.

## Capabilities and Constraints

- Stack is fixed: Next.js (App Router) + Tailwind, no new runtime dependencies without
  a one-sentence justification. Fonts load through `next/font`.
- The browser only ever talks to same-origin `/bff/*`; the Next.js server proxies to
  the backend over private DNS. Auth must sit in front of both the pages and `/bff/*`.
- **Auth is static and server-checked**: one operator credential from environment
  variables, verified in a route handler, carried in a signed httpOnly cookie. No user
  store, no registration, no password reset. It exists because the tool is internal
  but its load balancer is public.
- Live updates arrive as SSE events: `plan`, `task`, `token`, `interrupt`, `final`,
  `error`, `done`. SSE has no replay; `GET /sessions/{id}` rebuilds the screen.
- The backend never reports "needs input" as a task status; the frontend derives it
  (`components/Status.tsx::displayStatus`). Keep that function and `restore()` in
  `app/page.tsx` — they are verified behaviour, not styling.
- Every line must be explainable in an interview (`/CLAUDE.md`): small surface,
  why-comments, rejected alternatives recorded at the decision site.

## Brand Commitments

- Name: **Control Tower**. Its own neutral identity — no bolttech name, logo or
  colours anywhere in the UI (the repository is public).
- Must NOT read as a ChatGPT clone (centred bubbles + conversation sidebar hides the
  plan, which is the point) and must NOT read as a dark hacker console (near-black,
  neon accent, mono everywhere).
- Standing visual preference (owner, 2026-09-21): the category standard — a clean
  light SaaS CRM — played straight, not a learned metaphor. Craft bar: Untitled UI
  *Customers*, Shakuro *Contacts*, Dstudio *Plan*. Familiar must never mean faked:
  no nav to pages and no metrics the backend cannot serve.
- Voice: plain, operational, specific. States what happened and whose turn it is. No
  exclamation marks, no "AI magic" language, no apologies.

## Evidence on Hand

- Real seeded data: customers CUST-1001…1004, claims CLM-5001…5003, five policy
  excerpts (`data/seed/seed.sql`). Demo script: `docs/demo.md`.
- Verified screenshots of every functional state:
  scratchpad `ui/01`–`09` (pre-redesign look).
- There are no customers, testimonials, metrics, pricing or uptime figures. None may
  be invented — including on the sign-in page.

## Product Principles

1. Show the work, not just the answer.
2. Whose turn it is must never be ambiguous.
3. Claim only what was observed — no optimistic spinners, no "reconnected" that was
   not seen to reconnect.
4. System problems never speak in the assistant's voice.
5. Less surface, fully explained, beats more surface.

## Accessibility & Inclusion

WCAG 2.2 AA: 4.5:1 text contrast, status never by colour alone (glyph + label),
full keyboard operation with visible focus, `prefers-reduced-motion` honoured, live
regions for streamed status. Must stay legible in a compressed 1080p screen recording.
