---
title: "feat: make the three workflows visible to a reviewer alone with the app"
type: feat
status: completed
date: 2026-09-22
origin: docs/brainstorms/2026-09-22-three-workflows-visible-brainstorm.md
---

# feat: make the three workflows visible to a reviewer alone with the app

## Overview

The backend already coordinates three workflows the way the brief asks: one task
per workflow, a plan that extends across turns, a state slice per workflow, and a
shared customer id that lets "they" resolve without being named. The screen does
not show it. A reviewer clicking around the deployed app with no narration sees
prose answers, a plan panel that names workflows only in small print, and landing
prompts that name customer ids rather than capabilities.

Four presentation changes, all rendered from data the frontend already receives
or can fetch from the existing session endpoint (see brainstorm:
`docs/brainstorms/2026-09-22-three-workflows-visible-brainstorm.md`):

1. Workflow chips at the head of each assistant turn's step trail.
2. A Session card above the Plan: the customer and claims in context, then three
   always-present rows (Onboarding, Claims, Knowledge) with each workflow's
   latest outcome.
3. `workflow_states` fetched at the end of every turn from `GET /sessions/{id}`.
4. The empty state organised as the three workflows, one runnable prompt each,
   plus the path across all three in one session.

Frontend only. No graph, planner or SSE change, so `make verify-resume` is not
in scope.

## Problem Statement

`frontend/PRODUCT.md:12` states the rule this plan serves: "a backend property
that is true but not visible on screen scores as absent." Today:

- The step trail (`ChatPanel.tsx:192-199`) reads "Planned 2 tasks › looked up the
  claim › answered" and never names a workflow. The handoff between Onboarding,
  Claims and Knowledge is invisible in the transcript.
- The carried context is invisible. "Do they have any other open claims?" works
  because onboarding published `customer_id` (`onboarding/graph.py:96-101`), but
  nothing on screen says which customer the session knows about.
- Each workflow's result is prose only. `GET /sessions/{id}` returns
  `workflow_states` (`chat.py:164`) and the frontend ignores it
  (`Workspace.tsx:193-199`, `types.ts:42-48` has no field for it).
- The sidebar legend (`Workspace.tsx:520-533`) counts tasks per workflow and is
  hidden below `lg`. It says less than three outcome rows would, in a second place.
- The empty state (`ChatPanel.tsx:11-16`) lists four prompts by customer id.

## Proposed Solution

### 1. Workflow chips per turn (`Workspace.tsx`, `ChatPanel.tsx`, `types.ts`)

- `ChatMessage` gains `workflows?: Task["workflow"][]` beside `steps`, with the
  same client-only caveat: built from streamed events, absent after a restore.
- In `send()`, a per-turn `workflows: Task["workflow"][]` array next to `steps`
  (`Workspace.tsx:303`). Appended, deduplicated, in first-seen order, from the
  `plan` event's tasks and from every `task` event. The `task` source matters: a
  turn that resumes a paused workflow gets no `plan` event (`chat.py:46-49`), so
  without it the resumed turn would show no chip.
- `pushAssistant` carries `workflows` on every rewrite, like `steps`
  (`Workspace.tsx:305-314`). The `task` handler calls `pushAssistant` when the
  array grew; today it does not push at all (`Workspace.tsx:368-383`) and the
  chip would only appear once the next step or token arrived.
- `ChatPanel` renders the chips as the first fragment of the trail line: for each
  workflow, the dot (`aria-hidden`) and the capitalised name as text, then the
  existing steps joined by " › ". Not a `Chip` pill: pills mean status
  (`DESIGN.md` §3). The dot is never alone (`TaskTimeline.tsx:25-26`).
- `WORKFLOW_DOT` stays exported from `TaskTimeline.tsx`; `ChatPanel` imports it.

### 2. Session card (`WorkflowPanel.tsx`, `types.ts`)

A third card in the right column, above Plan, with its own `h2` "Session" (a
small label above the Plan heading would be a kicker, banned in `DESIGN.md` §9;
a card inside `CARD` would be a nested card, also banned).

**Context line.** Customer ref and name from `workflow_states.onboarding.customer`
(`external_ref`, `full_name`; `onboarding/graph.py:88-91`), else from the first
claims row that carries `customer_ref` / `customer_name` (only `get_claim` rows
do: `repository.py:60-62`). Claim refs from `workflow_states.claims.claims[]`.
When nothing is known: "No customer identified yet." When onboarding ran for an
unknown customer, `customer` is `null` and `customer_ref` is set
(`onboarding/graph.py:88-91,104`): print "CUST-9999, not found", never read
`full_name` off null.

Known limit, stated at the decision site: a claims-only turn that names the
customer in args reaches `get_active_claims`, whose rows carry no customer
(`repository.py:38-52`), and the claims slice does not record the ref. The line
then shows the claims only. Fixing it is one field in the claims slice, a graph
change, and out of scope here.

**Three rows**, fixed order Onboarding, Claims, Knowledge. Each row is dot + name
on the left and one line of text on the right, derived in one function:

```ts
// WorkflowPanel.tsx — the row is the LATEST task of that workflow, summarised.
// Reusing displayStatus keeps the vocabulary identical to the timeline: a row
// cannot say Running while the task above it says Last seen running.
function rowText(workflow, tasks, states, pending, live): string
  latest = last task with task.workflow === workflow
  if none              → "Not used yet"
  shown = displayStatus(latest, pending, live)
  if shown === "done"  → OUTCOME[workflow][states[workflow]?.outcome] + fact,
                          falling back to STATUS.done.label while the refetch
                          has not landed
  else                 → STATUS[shown].label   // Running, Needs you, Failed …
```

Outcome labels and the one fact per outcome, from the values each graph writes:

| Workflow | `outcome` | Row text |
|---|---|---|
| onboarding | `application_created` | Application created |
| onboarding | `manual_review` | Manual review · `review_reason` |
| onboarding | `rejected` | Rejected · `reasons` joined |
| onboarding | `customer_not_found` | Customer not found |
| claims | `summarised` | `claim_ref` + status with underscores as spaces, one per claim |
| claims | `information_incomplete` | Still awaiting: `missing_fields` as words |
| claims | `no_claims` | No open claims |
| knowledge | `answered` | Answered from N passages (`citations.length`, every passage retrieved) |
| knowledge | `no_matching_policy` | No policy document covers it |

An unmapped outcome prints the raw string with underscores as spaces rather than
nothing (brainstorm: a missing row on a used workflow reads as a bug).
`next_actions` are full sentences with citations (`claims/graph.py:98-130`) and do
not fit a row; they stay in the answer.

Rows use plain `ink-2` text and no colour: the row is a fact line, the timeline
directly below is the status machine, and a second amber element in the column
would dilute the one that needs a person (`DESIGN.md` §1 rule 3, §5). The arc
glyph accompanies "Running" so the two live indicators match.

**While connecting.** With a stored session id the restore can take up to two
minutes (`Workspace.tsx:145-167`). Until `conn === "ready"` the card prints
"Connecting…" instead of three false "Not used yet" rows.

**Narrow layout.** `WorkflowPanel` is `contents` below `lg` with explicit
`order-*` (`WorkflowPanel.tsx:52-57`). The Session card gets `order-1` and sits
before Plan in DOM order, so a phone reads Session, Plan, conversation, Activity.

### 3. Fetching `workflow_states` (`Workspace.tsx`, `types.ts`)

- `SessionView` in `types.ts` gains `workflow_states: WorkflowStates`, a narrow
  type naming only the keys the card reads, mirroring `schemas.py:32-43`.
- New `states` state in `Workspace`, reset by `newSession`.
- `restore()` sets it from the session it already fetched (`Workspace.tsx:193`).
- A separate `syncStates(id)`: one GET of `/bff/sessions/${id}`, writes only
  `states`, silent on failure (the card keeps the last known outcome; transport
  problems already speak in the notice). It must not be `restore()`, which
  replaces `messages` and would erase every trail and chip at the end of every
  turn, and posts "Session restored from checkpoint" each time.
- Called after the stream ends on a terminal event, `done` **or** `error`. An
  `error` ends the stream without `done` (`chat.py:104-109`); refetching only on
  `done` would leave the previous turn's outcome under a task row that says
  Failed. Awaited before `setBusy(false)` so New session cannot be pressed while
  the old session's states are still in flight, and guarded by the existing
  `restoreRun` counter so a stale response never lands in a newer session
  (`Workspace.tsx:120-124`).
- Rejected (brainstorm): a `state` SSE event on every node update. The slice
  carries retrieved chunks and is the wrong shape for a live channel; rows are
  outcomes, and outcomes exist at the end of a turn.

### 4. Sidebar legend removed (`Workspace.tsx`)

The "Workflows in this session" section, the `WORKFLOWS` constant and the
`WORKFLOW_DOT` import go; the sidebar comment (`Workspace.tsx:498-502`) and
`DESIGN.md` §4 (diagram and sidebar paragraph) are updated. The rows replace it
and are visible at every width, which the legend was not.

### 5. Empty state (`ChatPanel.tsx`)

`PROMPTS` becomes three entries keyed by workflow, each `{workflow, prompt,
hint}`, rendered as a group: dot + workflow name as a non-pressable heading (no
accent: it cannot be pressed, `DESIGN.md` §1 rule 2), the prompt as the existing
accent prefill button, the hint beneath. Prompts (brainstorm, resolved):

- Onboarding: "Onboard CUST-1001 and check whether they already have an active
  claim." — plans two tasks, no pause.
- Claims: "Summarise claim CLM-5003." — pauses for missing details.
- Knowledge: "When does a motor claim need a second review?" — answers with
  citations.

Below the list, one sentence: they work in one session — onboard first, then ask
"Do they have any other open claims?", then the policy question; the plan grows
and the customer carries over. Prefill only, never auto-send (brainstorm).

"Onboard CUST-1002" leaves the list: it exists for the crash beat, which needs a
backend to kill and belongs to the narrated demo (`docs/demo.md` Beat 3). The
comment at `ChatPanel.tsx:8-10` changes from "copied verbatim from docs/demo.md"
to "the first request of each of docs/demo.md's three workflows".

### 6. Documentation

- `frontend/DESIGN.md`: §4 diagram and sidebar text (legend gone, Session card
  added), §6 Conversation (chips) and Plan (Session card), a row in the rejected
  table for record cards in chat and per-workflow tabs (brainstorm).
- `docs/demo.md` Beat 1 and Beat 2 "what to point at": the Session rows and the
  context line, which is where "they" is seen to resolve.
- `frontend/PRODUCT.md:73-75` mentions `app/page.tsx` for `restore()`; it lives in
  `Workspace.tsx`. Fix while there.

## Technical Considerations

- **No new files in `frontend/components`.** Chips render in `ChatPanel`, rows in
  `WorkflowPanel`, the label map beside them. Same gate as the 2026-09-21 plan.
- **Typing `workflow_states`.** Backend sends `dict[str, Any]`. The TS type names
  only what the card reads (`customer.external_ref`, `customer.full_name`,
  `outcome`, `review_reason`, `reasons`, `claims[].claim_ref`, `claims[].status`,
  `claims[].customer_ref`, `claims[].customer_name`, `missing_fields`,
  `citations`). Every access is optional-chained: a slice mid-run has no
  `outcome`, and `retrieve` and `load_customer` replace their slice
  (`claims/graph.py:196-200`, `onboarding/graph.py:84-91`).
- **Knowledge merges rather than replaces** (`knowledge/graph.py:56`), so a second
  question inherits the first's `outcome` until `generate` runs. The row reads
  from the latest task's display status first, so a running or failed second
  question never shows the first's "Answered from 2 sources".
- **Restored turns have no chips**, exactly as they have no trail; the rows and
  context line survive a restore because they come from the checkpoint.

## System-Wide Impact

- **Interaction graph.** `done`/`error` → `syncStates` → one extra GET per turn
  through the BFF to `GET /sessions/{id}` → `graph.aget_state`. One checkpoint
  read; no write.
- **Error propagation.** A failed `syncStates` is swallowed: the card keeps the
  previous outcome and the notice already reports transport problems. A failed
  send never reaches it (no stream opened).
- **State lifecycle.** `states` is browser state only, reset with the session;
  a stale response is dropped by the `restoreRun` guard.
- **API surface parity.** `GET /sessions/{id}` is the only reader; the TS type
  now mirrors the whole pydantic model.
- **Cross-layer scenarios** (manual, in the browser, against the seed):
  1. Worked example → chips "Onboarding · Claims", Onboarding row "Manual
     review · existing active claim(s): CLM-5001", Claims row lists CLM-5001,
     context line "CUST-1001 Priya Raman".
  2. "Do they have any other open claims?" → Claims chip only, context line
     unchanged, Claims row updated, Onboarding row unchanged.
  3. Policy question → Knowledge chip, "Answered from N sources".
  4. "Summarise claim CLM-5003." → Claims row "Needs you"; answer → "CLM-5003
     under review"; skip → "Still awaiting: incident report, police reference".
  5. Refresh mid-pause → rows and context line rebuilt, chips absent, question
     card open.
  6. Backend killed mid-run, page refreshed → rows say "Last seen running", not
     "Not used yet".

## Acceptance Criteria

- [x] Every streamed assistant turn shows the workflow(s) that ran it at the head
      of its trail, including a resume turn with no `plan` event.
- [x] The Session card shows three rows from first paint, "Connecting…" while a
      stored session restores, and never "Not used yet" for a workflow with a
      task in the plan.
- [x] Row text follows `displayStatus` for non-terminal tasks and the outcome map
      for done tasks; unmapped outcomes print the raw string as words.
- [x] Context line names the customer from onboarding or from a `get_claim` row,
      handles `customer: null`, and lists claim refs.
- [x] `workflow_states` is fetched after `done` and after `error`, never via
      `restore()`, awaited before `busy` clears, dropped if the session changed.
- [x] Sidebar legend, `WORKFLOWS` and the `WORKFLOW_DOT` import are gone from
      `Workspace.tsx`.
- [x] Empty state shows three workflow groups with one prefill prompt each and
      the one-session path sentence; nothing auto-sends.
- [x] `npm run typecheck`, `node --test lib/sse.test.mjs`, `npm run build` pass.
- [x] Scenarios 1 to 5 above verified in a browser against the seed on
      2026-09-22 (Nova Pro); screenshots in `.playwright-mcp/ui/`. Scenario 6
      (kill mid-run) not run: it needs the container stopped mid-turn and the
      `stalled` branch is the existing `displayStatus` path.
- [x] `DESIGN.md`, `PRODUCT.md`, `docs/demo.md` updated as in §6.

## Dependencies & Risks

- Local verification needs Postgres, the seed, an embedded corpus and an LLM
  credential (`docs/demo.md` Setup). Scenario 4 mutates CLM-5003; the reset SQL
  is in `docs/demo.md`.
- Nova Pro routing is not deterministic; the chips reflect what the planner
  actually chose, which is the point.

## Sources & References

- **Origin brainstorm:** `docs/brainstorms/2026-09-22-three-workflows-visible-brainstorm.md`.
  Carried forward: presentation over existing data; Session panel with three fixed
  rows and context header, replacing the sidebar legend; chips on the assistant
  turn only; refetch at turn end rather than a `state` SSE event; three-card empty
  state with the worked example, CLM-5003 and the policy question; seed mutation
  accepted and explained by the row.
- Previous plan and its gates: `docs/plans/2026-09-21-feat-readable-answers-step-trail-claims-plan.md`.
- Design rules: `frontend/DESIGN.md`; product rules: `frontend/PRODUCT.md`.
- Session endpoint: `backend/app/api/chat.py:125-166`, `backend/app/api/schemas.py:32-43`.
- State each workflow writes: `backend/app/graph/workflows/onboarding/graph.py:84-104,278-295`,
  `claims/graph.py:163-252,505-520`, `knowledge/graph.py:110-153`.
