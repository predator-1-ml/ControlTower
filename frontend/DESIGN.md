# Control Tower — design

Status: **built and verified** (2026-09-21, second design). This file describes the
interface as it exists in code; where the two disagree the code is right and this
file is a bug. Product truth (who it is for, what it may not claim) is in
`PRODUCT.md`.

Method: the owner browsed Dribbble's "crm" results, then answered four questions
(look, plan view, scope, fate of the old design) and picked the accent from four
options switched live over the running app. Built with the `impeccable` skill
(pbakaus/impeccable 4.3.1: product record, direction contract, craft floor, its
detector at 0 findings, an independent finish review) and checked against the
`design-taste-frontend` skill's bans. The direction was pinned by the owner, so
impeccable's random direction roll was not used to choose it.

This replaces *Flight Progress Strips* (tasks as paper strips moving between bays
on a metal rack). It was coherent, but it was a metaphor that had to be learned
first. The owner asked for the look an operations handler already knows.

---

## 1. The idea

**A clean light CRM workspace where the plan is a record's activity timeline.**
Familiar on purpose: someone who has used any CRM reads it without being taught.

Three rules carry the whole interface:

1. **Surfaces are two greys apart.** White is where work happens (conversation,
   task rows, inputs); `canvas` is the frame around it (sidebar, plan column,
   sign-in ground). Hairline borders separate; one soft shadow lifts a card.
2. **The accent is only ever something the operator can do or did.** The primary
   button, the focus ring, their own messages, the suggested requests, the caret
   and the selection. Never decoration, so the accent on screen always means "yours".
3. **State hues mean one thing everywhere.** Amber needs you, green done, red
   failed. Every use is a glyph + a word on a wash, never colour alone. Running
   work is ink, not coloured: colour is reserved for what is news.

| Rejected | Why |
|---|---|
| Sidebar nav to Customers / Claims / Reports, KPI cards (what every Dribbble CRM shot has) | The backend has two endpoints, `POST /chat` and `GET /sessions/{id}`. Pages and metrics would be invented data, which `PRODUCT.md` forbids, or new backend surface to defend. |
| Pipeline kanban (Planned / Running / Needs you / Done columns) | Four columns do not fit beside a conversation at 1280px, and three tasks over four columns is mostly empty boxes. The owner chose the timeline. |
| Soft glassy pastel CRM (the RonDesignLab shots) | Blur smears in a compressed recording and its contrast does not reach AA. |
| Dark sidebar, light content | A second ground to tune contrast on, for no information. |
| Flight Progress Strips (the previous build) | A metaphor to learn before the screen can be read. |

---

## 2. References

Only what was actually opened and looked at; one transferable idea each.

| Source | Idea taken | Where it lands |
|---|---|---|
| Dribbble "crm": Jordan Hughes, Untitled UI *Customers* | Quiet grey sidebar, white working area, hairline cards, status as small pills | The shell (`Workspace.tsx`), `Chip` in `Status.tsx` |
| Dribbble "crm": Shakuro *Contacts* | Restraint: near-monochrome with colour only in status tags | Rule 3; running work stays ink |
| Dribbble "crm": Dstudio *Plan* (deal page) | A record's activity as a vertical timeline beside the main work | `TaskTimeline.tsx` |
| Temporal Web UI docs | Event history as one-line timed entries | The activity log |
| Inngest run inspector docs | The error message is printed in the run's row | `task.error` is printed in the task row; a recording cannot hover a tooltip |
| Railway deployment docs | "Crashed" is a first-class state that carries an action | "Backend unreachable" always carries **Retry now** |
| Grafana state timeline docs | Print the state as text; colour second | Every status has a word |
| impeccable's dealt "warm consumer app surface" world (declined) | One discipline kept: the action colour appears only on actionable elements | Rule 2 |

---

## 3. Tokens

Tailwind 4 is CSS-first: every `--color-x` in `app/globals.css` becomes `bg-x`,
`text-x`, `border-x`. There is no `tailwind.config`. **Light only**, chosen from the
scene: a lit office, a desk monitor, a compressed recording.

Contrast is computed (WCAG relative luminance), not estimated.

| Token | Value | Role | Contrast |
|---|---|---|---|
| `canvas` | `#f6f7f9` | the frame: sidebar, plan column, sign-in ground | — |
| `surface` | `#ffffff` | where work happens: conversation, cards, inputs | — |
| `sunken` | `#eef0f4` | hover, neutral chips, marked record ids | — |
| `line` | `#e3e6eb` | hairline borders (decorative) | — |
| `line-strong` | `#848d9a` | control borders | 3.4 on surface (WCAG 1.4.11 asks 3) |
| `ink` | `#131720` | text | 17.9 |
| `ink-2` | `#454d5b` | secondary text | 8.5 surface · 8.0 canvas · 7.5 sunken |
| `ink-3` | `#5f6877` | times, placeholders, group labels | 5.6 surface · 5.3 canvas |
| `accent` / `-deep` / `-wash` | `#2451c6` / `#1b3e9c` / `#e8eefc` | the operator (rule 2) | 6.9 on surface · deep on wash 8.2 |
| `hold` / `hold-deep` / `hold-wash` | `#9a4a06` / `#7a3a05` / `#fdf1e1` | needs a person — the only warm colour; deep is the amber button's hover | 6.3 · 5.6 on wash · 5.8 on canvas · white on deep 8.6 |
| `clear` / `clear-wash` | `#1a6a3a` / `#e5f4ea` | done | 6.6 · 5.8 on wash |
| `fail` / `fail-wash` | `#b3261e` / `#fdeceb` | failed | 6.5 · 5.7 on wash |
| `wf-onboarding` / `-claims` / `-knowledge` | `#5b82c9` / `#c4983d` / `#9078b8` | workflow dot (never alone: the name is printed beside it) | — |

**Colour strategy: Restrained.** One cool-grey neutral family, one accent, three
state hues. Accent **Cobalt**, chosen by the owner from four options switched live
over the app: cobalt, teal (the previous accent), ink-only, indigo. Cobalt is the
furthest from all three state hues; teal sat close to the green Done chip.

**Type: one family.** Public Sans via `next/font`, self-hosted at build time so no
browser ever calls Google. Drawn for government service UIs: neutral, sturdy at
12–14px where chips and the log sit, with tabular digits for ids and clock times.
Rejected: a mono for ids (`tabular-nums` aligns the digits; mono would be a
costume) and Inter / Geist (what every AI tool ships in). Fixed rem sizes, Tailwind
defaults; no fluid type — this is a tool.

**Shape and depth.** Two radii with a rule: cards and controls are 8px
(`rounded-lg`); status chips and round badges are full pills. So a pill always
reads as "a status", never as a button. One shadow, `shadow-card`: offset, blurred,
ink-tinted. Disabled controls are an explicit grey pair (`sunken` / `ink-3`,
4.9:1), not opacity: a faded label is unreadable in a compressed recording.

---

## 4. Layout

```
≥1024px                                                    fixed height, panes scroll
┌────────────┬──────────────────────────────────────────┬──────────────────────┐
│ Control    │ Conversation                             │ ┌ Plan   1 of 2 done ┐│
│ Tower      ├──────────────────────────────────────────┤ │ (t1) Onboard  ✓Done ││
│ [● status] │        ┌──── 48rem column ─────┐         │ │  │                  ││
│ [New       │        │      [ operator msg ] │         │ │ (t2) Retrieve ⏸Needs││
│  session]  │        │ (CT) Control Tower    │         │ └────────────────────┘│
│            │        │      answer text      │         │ ┌ ▾ Activity ────────┐│
│ Workflows  │        │ ┌ Claims needs an     │         │ │ 22:09:07 plan · 2   ││
│ ● Onboard 1│        │ │ answer from you     │         │ └────────────────────┘│
│ ● Claims  1│        │ └─────────────────────│         │                      │
│ ● Knowl.  0│        └───────────────────────┘         │                      │
│            ├──────────────────────────────────────────┤ session …            │
│ operator   │        [ write a request…     ] [Send]   │ trace …              │
│ [Sign out] │                                          │                      │
└── 15rem ───┴──────────────────────────────────────────┴──────── 25rem ───────┘

<1024px: the page scrolls; the sidebar becomes a top bar (name, status, New session,
Sign out; the workflow legend is dropped because every row prints its workflow).
Order: top bar → notice → PLAN → conversation → activity → ids. Composer is sticky.
```

The sidebar has no navigation links: there is one screen, and a link that leads
nowhere is a lie in the UI. It holds only things that are real — what this browser
has observed of the backend, the way to start over, which workflows this session
has used (live counts from the plan), who is signed in.

The conversation is **one centred 48rem column** shared by the heading, every turn,
the question card and the composer. Without it the operator's bubbles hugged the
pane's far edge while answers stopped at their own measure, and on a wide monitor
the two speakers were not in the same conversation.

The plan comes first on a phone because it is short and it is the evidence; under
the transcript it would never be seen.

---

## 5. Status vocabulary (`components/Status.tsx`)

The backend only ever writes `running` for an in-flight task — an `interrupt()`
leaves it `running` because the node never returned. Two states that matter most
are therefore derived in the browser by `displayStatus()`:

| Wire status | Condition | Chip | Glyph | Tone |
|---|---|---|---|---|
| `pending` / `ready` / `blocked` | — | Planned / Ready / Blocked | ring | quiet (grey) |
| `running` | a stream is open | Running | spinning arc | busy (grey, ink text) |
| `running` | its workflow has an open question | **Needs you** | pause | hold, **filled**; row tinted and enlarged |
| `running` | no stream open | Last seen running | half disc | quiet |
| `done` | — | Done | check | clear |
| `failed` | — | Failed + error printed in the row | cross | fail |
| `skipped` | — | Skipped | dash | quiet |

"Last seen running" exists because after a crash the checkpoint says `running`
forever; an animated row would claim liveness this browser has not observed.

The sidebar's **status chip** reports the session, worst news first: Backend
unreachable → Connection lost, retrying → Live, streaming → Waiting on you →
Connecting → Ready. News gets a filled chip and calm states a grey one, so a filled
chip always means "look". It is the same `Chip` the task rows use.

---

## 6. Surfaces

**Sign-in (`app/sign-in/page.tsx`)** — a server component with a plain HTML form; it
ships no JavaScript of its own. One card on the `canvas` ground, the same card the
plan sits in inside. Label above input, error under the form, accent button (signing
in is the operator's act). One error message for wrong username *or* password. No
sign-up and no "forgot password": there is no user store. Rejected: a split page with
a marketing panel — there is nothing true to put in it.

**Conversation (`ChatPanel.tsx`)** — the white column. The operator's turns are
accent-on-wash bubbles, right-aligned; the system's are named ("Control Tower", a
round CT badge) and unboxed, so every answer starts from the same left edge. Under
the name, a quiet **step trail** — `Planned 1 task › looked up the claim › needs your
answer`, a spinning arc while the turn is live — so process is *shown* and the answer
below is only the answer. A turn that paused has a trail and no text at all. Record
ids (`CUST-1001`, `CLM-5003`) and citations (`[claims-handling-policy.md, Escalation]`)
are marked inline and never wrap, so a long answer can be scanned for which customer,
which claim, and where a statement came from. The empty state offers the demo
script's requests as fill-the-composer buttons — they do not send, so a presenter
can narrate first. When a workflow holds, its question is the **largest thing in the
conversation** (scale follows urgency), the input gains an amber ring, and the button
turns amber and says **Send answer** — because the next message *is* the answer and
cannot be anything else (`docs/tradeoffs.md`). The card says *where* to answer (the
composer is at the foot of a tall pane and a placeholder alone was missed) and
carries the one way out, **I don't have this yet**, which sends an empty answer.

**Plan (`TaskTimeline.tsx`, `WorkflowPanel.tsx`)** — one row per task in **plan
order, never re-sorted**: "after t1" always points up the joining line. Row anatomy:
id badge on the line · action + workflow dot and name + dependency ("waits for t1" /
"after t1") + any error · status chip. The empty state teaches the panel in two
sentences instead of saying "nothing here". Below it, the activity log (native
`<details>`, browser clock, capped at 50 lines) and the selectable session / trace
ids (the join key to `audit_events`).

**Notice (`Workspace.tsx`)** — the one transient surface, a full-width bar above the
panes. Transport problems speak here, never as the assistant. It says "Backend is
back" only if this browser watched it go away.

---

## 7. Motion

One authored motion: **a task row changing state**, via the View Transitions API.
Each row sets `view-transition-name: task-<id>`; `move()` wraps the state change in
`document.startViewTransition(() => flushSync(...))`, so the browser crossfades that
row and slides its neighbours when the needs-you row grows. 200ms, exponential
ease-out, no bounce; the page root is excluded so only rows animate. Bursts of task
events supersede each other's transitions, whose rejected promises are swallowed
deliberately.

No animation library and no position maths. Without the API (Firefox, older Safari)
or under `prefers-reduced-motion`, rows simply change — the chip's word is the state;
movement only confirms it. The only other motion is the spinning arc while a request
is in flight and the caret on a streaming answer; both stop under reduced motion.

---

## 8. Accessibility

WCAG 2.2 AA. All text ≥ 4.5:1 (table above); control borders ≥ 3:1. Status is glyph
+ word + colour, never colour alone. One `:focus-visible` ring for every control.
The composer is labelled and is never disabled (disabling drops focus every turn;
only submitting is blocked). The conversation is `role="log"` with `aria-busy` while
streaming, so a finished answer is announced once, not per token; speakers are named
for screen readers. The open question is `role="alert"`; the status chip and the
notice are `role="status"`. Glyphs, dots and the CT badge are `aria-hidden` — the
word beside them is the name.

---

## 9. Banned here

Nav links or pages the backend cannot serve · KPI cards or any invented number ·
kicker labels above headings · gradient text · glass/blur as decoration · thick
coloured side-borders on callouts · nested cards · pulsing status dots · Unicode or
emoji as icons · monospace as a "technical" costume · modals · the accent on
anything the operator cannot press and did not write · optimistic spinners for
states the browser has not observed · any claim (customers, uptime, metrics) that
`PRODUCT.md` does not list as real.
