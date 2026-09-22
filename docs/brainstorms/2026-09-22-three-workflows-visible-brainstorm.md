---
date: 2026-09-22
topic: three-workflows-visible
---

# Making the three workflows visible to a reviewer who is alone with the app

## What We're Building

The brief is satisfied in the backend: the planner emits one task per workflow,
a follow-up turn extends the plan, each workflow keeps its own slice of
`workflow_states`, and `customer_id` is shared so "they" resolves without being
told. The problem is that a reviewer clicking around the deployed app with no
narration cannot see any of that. The conversation column never names a
workflow; the carried context is invisible; each workflow's result is prose
only; and the landing prompts name customer ids rather than capabilities.

Four presentation changes, all over data the frontend already receives:

**1. Workflow chips on every assistant turn.** The step trail above each answer
starts with the workflow(s) that handled that turn, from the `task` events of the
turn: "Onboarding › Claims" then "Planned 2 tasks › …". The transcript then reads
as a sequence of handoffs, which is the requirement in one glance.

**2. A Session panel with the context in play.** The right-hand Plan panel gets a
header naming what the session currently knows: the customer (ref and name) and
the claim(s) touched. This is what makes "Do they have any other open claims?"
visibly answered from shared state rather than from luck.

**3. Three always-present workflow rows with outcomes.** Under the header, one
row per workflow, present from the first paint. Untouched: "Not used yet".
Touched: a plain-language outcome mapped from the workflow's `outcome` string
plus one fact (Onboarding: "Manual review, active claim CLM-5001"; Claims:
"CLM-5003 under review, escalate to duty manager"; Knowledge: "Answered, 2
sources"). The task timeline stays beneath. The greyed rows are the invitation:
a reviewer who has used two workflows can see there is a third.

**4. Empty state organised as the three workflows.** Three cards, one runnable
prompt each, and beneath them the suggested path that crosses all three in one
session (onboard, then "Do they have any other open claims?", then the policy
question). The sign-in blurb already says "onboarding, claims and policy"; the
landing page should say it louder.

## Why This Approach

| Considered | Verdict |
|---|---|
| Session panel + per-turn chips, empty state as three cards | **Chosen.** Every element renders from `task` events or from `workflow_states`, which `GET /sessions/{id}` already returns. Coverage is visible before anything runs. |
| Structured record cards inside the conversation | Rejected: needs a new payload shape or prose parsing, duplicates the answer, and a restored session would have to replay them. |
| One tab per workflow in the right panel | Rejected: hides two of three at any time, the opposite of showing coverage. |
| A guided tour / scripted walkthrough overlay | Rejected: a second UI to defend, and it narrates instead of showing. |
| Naming the workflow inside the answer prose | Rejected: the 2026-09-21 brainstorm deliberately kept process out of the prose; the chip is the process view. |

## Key Decisions

- **Presentation only, no new state.** Chips come from the `task` events already
  streamed per turn; the panel comes from `workflow_states` and the plan. No
  workflow or planner change, so nothing to re-verify with `make verify-resume`.
- **Outcome rows are a lookup, not a template engine.** One frontend map from
  `(workflow, outcome)` to operator words, in the same spirit as the backend's
  step label map. An unmapped outcome shows the raw string rather than nothing,
  because a missing row on a used workflow would read as a bug.
- **The three rows are fixed, in brief order: Onboarding, Claims, Knowledge.**
  They replace the sidebar "Workflows in this session" legend, which today says
  the same thing with less. One place, not two.
- **Panel data refreshes at turn end, from the session endpoint.** Rejected: a
  new `state` SSE event carrying `workflow_states` on every node update. That
  slice includes retrieved chunks and is the wrong shape for a live channel; the
  rows are outcomes, and outcomes exist at the end of a turn.
- **Context header reads from `workflow_states`, not a new field.** Onboarding
  publishes the customer, claims publishes the claim list. If the session view
  needs `customer_id` for a turn that used only claims, that is one field added
  to `SessionView`, nothing more.
- **Chips reuse the existing workflow colour dots** from the task timeline, so
  the chip on a turn and the row in the panel are visibly the same thing.
- **The empty state prompts stay as prefill, not auto-send.** The reviewer should
  read the request before it runs; that is also what makes "the plan appears
  before anything runs" believable.

## Resolved Questions

- **Seed mutation from the claims prompt: accepted, the row explains it.** After
  the first pause-and-answer, the Claims row reads "CLM-5003 under review", so a
  second run reads as a consequence of the first. Rejected: a reset action (one
  more thing to defend, and `make seed` truncates the embedded corpus) and
  pointing the card at a claim that never pauses (loses the pause from the
  unsupervised path).
- **Chips on the assistant turn only.** They lead the step trail, tied to what
  actually ran. Repeating the routing on the user bubble says the same thing twice.
- **Card prompts.** Onboarding: "Onboard CUST-1001 and check whether they already
  have an active claim." (the brief's worked example, two workflows, no pause).
  Claims: "Summarise claim CLM-5003." (pauses for missing details). Knowledge:
  "When does a motor claim need a second review?". Only one of the three locks the
  session on a question, and it is the one whose pause matters most.

## Next Steps

→ `/ce:plan` for implementation details. Frontend-only unless `SessionView`
needs `customer_id`.
