---
title: "feat: answers a handler can read, a live step trail, and a claims pause that does something"
type: feat
status: active
date: 2026-09-21
origin: docs/brainstorms/2026-09-21-operator-answers-and-claims-realism-brainstorm.md
---

# Answers a handler can read, a live step trail, and a claims pause that does something

## Overview

Three changes, in this order, each shippable on its own
(see brainstorm: `docs/brainstorms/2026-09-21-operator-answers-and-claims-realism-brainstorm.md`):

1. **Answers** stop being a state dump and read like a colleague wrote them.
2. **Process is shown, not narrated**: a live step trail on each answer, sources as chips.
3. **The claims pause matters**: the LLM reads the operator's reply, the claim row
   changes, and the handling rules that today exist only in policy text run in code.

One sentence per phase for the interview:

- *Compose was handed `str(dict)` of raw state, so it narrated raw state; now it is
  handed operator-facing fact lines built in code, and it cannot leak what it never sees.*
- *What the system did is an event stream, not prose; the UI draws it as a trail.*
- *LLM for language, code for decisions: the model reads a human's free text, code
  verifies it, code applies the thresholds.*

## Problem Statement

Observed on the live app, 2026-09-21:

> "The task claims.retrieve_claims completed successfully. … The claim ID is
> 44d65b54-51f7-440a-a34a-f4832916bcde, … amount is 12750.0 …"

> "Task [t1] knowledge.answer_question completed successfully. The question "…" was
> answered with "…". Relevant citations were provided from multiple sources."

From the frontend it feels like a deterministic system, not an assistant. Root causes,
all verified in code:

| Symptom | Cause |
|---|---|
| Task names in prose | `compose.py:49` builds `[{id}] {workflow}.{action} -> {status}` and gives it to the model |
| UUIDs, raw chunks, stale `missing_fields` | `compose.py:58` passes `str(dict)` of the slice, excluding only `customer` and `claims`; `incomplete` (claim dicts with `id`) and onboarding's `application` (`id`, `customer_id`) get through |
| `12750.0` | `repository._serialise` turns `Decimal` into `float` so state survives JSON; nothing formats it back |
| No "so what" | `compose.py:31` says "Report only what the results below state"; nothing names the reader, the request, or a next step |
| Said twice | `STREAMING_NODES` streams `summarise` / `generate`, then `compose` paraphrases the same text |
| Claims pause is theatre | `claims/graph.py:186-191` stores the reply verbatim and ends; no validation, no DB write, no audit row; `docs/demo.md:70` says "Supply anything; it completes" |
| Rules only in prose | "5000 or more → second review" and "exceeds 10000 → duty manager" exist only in `data/seed/seed.sql:46-65`. `docs/assumptions.md:28-31` says code and policy agree. They do not. |

## Proposed Solution

### Phase 0 — probe before building (scratch, not committed)

`with_structured_output(..., include_raw=True)` is already proven on Nova Pro by the
planner (`planner.py:106`). Probe the **new** schema the same way, **twice**
(CLAUDE.md: one green call proves nothing), from **inside the backend container**
(`docker compose exec backend python …`) — never from the host while the container
is up, because `aws login` refresh tokens are single use and the two would kill each
other's session.

Schema to probe: `Reply(supplied: list[Supplied])` with `Supplied(name, value)` — a
list of pairs, not a free-form dict, because the field names vary per claim and a
fixed shape is what tool-calling models fill reliably.

Probe inputs: a clean reply ("Incident report IR-2291, police ref PR-77431"), a
partial one ("the police reference is PR-77431, still chasing the report"), and an
off-topic one ("what does this claim need again?"). Record the results in the plan's
Phase 3 notes before writing the node.

### Phase 1 — answers (backend `compose.py`, one frontend line)

**Presentation lives in one file.** Workflows keep publishing data; `compose.py` owns
how it is said. One small function per workflow turns a slice into fact lines:

```python
# backend/app/graph/compose.py  (sketch)
FACTS = {"claims": _claims_facts, "onboarding": _onboarding_facts}   # knowledge is never paraphrased, see below

def _money(amount: float) -> str:            # 12750.0 -> "12,750.00"; no currency: there is no currency column
def _day(iso: str) -> str:                   # "2026-09-01" -> "1 Sep 2026"; f"{d.day} {d:%b %Y}" — NOT "%-d", which does not exist on Windows
def _claims_facts(ws: dict) -> list[str]:    # claim_ref, type, customer name + CUST ref, status in words, money, day, received, still missing, next actions
def _onboarding_facts(ws: dict) -> list[str] # outcome in words, review_reason / reasons, application product + status. Never application["id"].
# _claims_facts must tolerate a missing customer name / ref: get_active_claims does not join the customer, get_claim does.
```

*Rejected:* a per-workflow allow-list of keys — it cannot format a float or reach
inside `application`. *Rejected:* each workflow publishing its own `report` lines —
eight terminal nodes to touch, including the verified, LLM-free onboarding workflow.

**What the model is given** (replaces `_summarise`'s output):

```
Operator's request: "Summarise claim CLM-5003."

Claims
- Claim CLM-5003: property claim for Tom Baker (CUST-1004)
- Status: Under review
- Amount claimed: 12,750.00
- Incident date: 1 Sep 2026
- Received from the operator this turn: incident report IR-2291; police reference PR-77431
- Next step: Escalate to the duty manager: the claim exceeds 10,000 [operations-runbook.md, Escalation]

Could not complete
- A knowledge task failed: <task.error>
```

- The request is the last `HumanMessage` in `state["messages"]`. A resume turn adds
  none (`chat.py:61` sends `Command(resume=…)`), so it is still the original request —
  which is the right thing to answer.
- Task lines lose `{workflow}.{action}` and `[tN]`. Failed and skipped tasks become a
  plain line with the error, so partial success is still reported (the existing
  docstring's "a half-succeeded request is a Tuesday" stays true).
- Turn scoping via `turn_task_ids` is unchanged.

**New system prompt** (draft — the prompt is the deliverable, tune it on the probe):

```
You are writing to an insurance operations handler who asked the request below.

Answer their request first, in the first sentence. Then the two to four facts that
matter to them. If a "Next step" is given, end with a line starting "Next step:".
If none is given, do not write one.

Rules:
- Use only the facts given. Do not infer, soften, or add advice.
- Refer to things the way the handler does: CLM- and CUST- references and names.
- Copy amounts, dates and anything in [square brackets] exactly as given.
- If something could not be completed, say so plainly. Never report only what worked.
- Plain text. Short lines. "- " for a list. No headings, no preamble.
```

**Text a workflow wrote is never paraphrased.** Two workflows produce operator-facing
text themselves: knowledge's `answer` (citations are built from the retrieved chunks,
`knowledge/graph.py:138-144`; a second model pass can only damage them, and a damaged
citation silently stops being a chip) and claims' `summary` (the assignment names
"claim summary generation" as a claims capability, so it stays in the claims
workflow). So, in code, per workflow that ran **this turn** and is `done` (both checks
matter: slices are merged, not replaced, so a stale `answer` from an earlier turn is
still there):

- if it was the turn's only task, its text **is** `final_response` and compose makes
  **zero** model calls;
- otherwise compose narrates the workflows that only produced facts (onboarding), and
  each written text is appended verbatim after the model's text, in plan order.

`_claims_facts` is therefore only used when claims ran but produced no summary (`no_claims`).

`compose` still returns the same dict, so `final` still fires from node `compose`
(`streaming.py:132`). The knowledge `SYSTEM_PROMPT` gains the same "answer first,
short" voice rules.

**Delete the dead branch.** `compose.py:68,89` handle `TaskStatus.NEEDS_INPUT`, and the
prompt has a "waiting on information" rule. Confirm with a grep that nothing ever sets
`NEEDS_INPUT` (an interrupted turn never reaches compose at all), then delete both.

**Streaming.** `token` frames gain `node`. `Workspace.tsx` restarts its `streamed`
draft when the node changes, so a multi-task turn that streams `generate` and then
`compose` no longer shows one answer glued to the next before `final` replaces both.

### Phase 2 — step trail and source chips

**Backend (`streaming.py`).** A new event joins the contract listed in the module
docstring:

```
step       a graph node the handler would recognise has finished
```

```python
# backend/app/api/streaming.py  (sketch)
STEP_LABELS: dict[tuple[str, str], str] = {
    ("claims", "retrieve"): "looked up the claim",
    ("claims", "validate"): "checked it for missing information",
    ("claims", "read_reply"): "read your reply",
    ("claims", "record_information"): "updated the claim",
    ("claims", "assess"): "applied the handling rules",
    ("knowledge", "retrieve"): "searched policy documents",
    ("knowledge", "generate"): "answered from the passages found",
    ("onboarding", …): …,            # one line per node a handler would recognise
    ("", "compose"): "wrote the answer",
}
# in the `updates` branch:  workflow = ns[0].split(":")[0] if ns else ""
#                           label = STEP_LABELS.get((workflow, node));  if label: yield sse("step", {...})
```

- Keyed by `(workflow, node)` because `retrieve` exists in two subgraphs. The workflow
  comes from the chunk's `ns` (`subgraphs=True` is already on, `chat.py:88`).
- **Labels are mapped in the backend**, so LangGraph node names never cross the wire
  (the module's rule: "the event names are the API; LangGraph's chunk shapes are not").
  An unmapped node emits nothing.
- `updates` arrive when a node *finishes*, so the trail is a list of completed steps.
  That is the honest reading of "live": principle 3 in `PRODUCT.md`, claim only what
  was observed.
- "Planned N tasks" is not a `step`: the frontend already gets `plan`.
- `request_information` is deliberately **not** in `STEP_LABELS`: its update is emitted
  when the node finishes, which is on the *resume* turn. "Needs your answer" is added by
  the frontend from the `interrupt` event it already receives.
- Pin the `ns` shape (`("claims:<id>",)`) with a test; it is LangGraph's, not ours.
- Passage count ("2 passages found") only if the `retrieve` update visibly carries
  `chunks`; otherwise leave it out rather than add a second mechanism.

**Frontend.**

- `lib/types.ts`: `ChatMessage` gains optional, client-only `steps?: string[]`.
- `Workspace.tsx::send`: collect `Planned N task(s)` from `plan` and each `step`
  label; `pushAssistant(text, steps)` creates the assistant turn as a placeholder on
  the first step, so the trail is visible before any token. Today `pushAssistant`
  (`Workspace.tsx:288-295`) rebuilds the message as `{role, text}`, which would drop
  `steps` on every token — it must carry them. An interrupted turn gets no `token` and
  no `final`, so its placeholder is a **trail-only** turn, rendered on purpose: the
  trail ending in "needs your answer", directly above the question card.
- `ChatPanel.tsx`: the trail is one quiet `text-sm text-ink-2` line under "Control
  Tower", steps joined by " › ", with the spinning arc while that turn is live. No new
  component file.
- **Source chips**: extend the existing `RECORD_ID` tokenizer to a second alternative
  for `[source, section]`, inside the **same single capture group**
  (`/(\b(?:CUST|CLM)-\d+\b|\[[\w-]+\.md, [^\]]+\])/`) — a second group would break
  the `index % 2` trick `withRecordIds` relies on — and render it with the same inline mark. Inline, where the
  citation occurs — it keeps claim-level attribution and **survives `restore()`**,
  because it is parsed from stored text. *Rejected:* a separate sources payload on
  `final` — restored messages are `{role, text}` and would lose it.
- Not replayed after `restore()`: the trail is what this browser observed, exactly
  like the activity log. `audit_events` stays the durable record.

### Phase 3 — claims

```
retrieve ─▶ not_found
         ─▶ validate ─▶ assess ─▶ summarise                     (nothing missing)
                     ─▶ request_information ─▶ read_reply ─┬─▶ request_information   (progress, still missing)
                                                           └─▶ record_information ─▶ assess ─▶ summarise
summarise ─▶ END
```

| Node | Kind | Does |
|---|---|---|
| `request_information` | interrupt | `interrupt()` stays the **first statement** (nothing above it: it re-runs from the top on resume). After resume it stores the raw reply in the slice and nothing else. It no longer marks the task `done`. It asks about **one claim, `incomplete[0]`**: `validate` unions the missing fields of every incomplete claim, and a supplied "police reference" would then have no owning claim. Say so in the docstring and pin it with a test (the seed has no such customer, so nothing else would catch it). The question is built from the slice's *current* `missing_fields`, so a second ask is shorter than the first. |
| `read_reply` | LLM | **Its own node so the reply is checkpointed before the fallible call** — that, not double execution, is the reason for the split (code after `interrupt()` runs once). `model.with_structured_output(Reply, include_raw=True)`, the planner's pattern. **Code then verifies**: keep a pair only if its name is in `missing_fields` *and* its value occurs in the reply (casefolded, whitespace-normalised). This guards against *invention* only — it would still accept "yes" as a police reference; the docstring says so. The node merges `supplied` into the slice and narrows `missing_fields`. **Any failure — bad parse *or* a transport error, which raises even with `include_raw=True` — is caught**: append an `errors` record and treat it as nothing extracted, with the distinct fact "the reply could not be read". Uncaught, the interrupt is already consumed, the task is still `running`, the next message gets planned as a new request, and the operator is asked again from `retrieve`. |
| *(route)* | code | Re-ask only if this reply supplied **at least one new field** and something is still missing. **No counter:** progress is the loop variant — fields are finite, so it terminates; a reply with nothing usable ends the pause and the claim truthfully stays `awaiting_information`. |
| `record_information` | DB write | The only write, in its own node. New `repository.record_claim_information(pool, claim_ref, received_names)`: **one statement**, idempotent and race-safe — `UPDATE claims SET missing_fields = missing_fields - %s::text[], status = CASE WHEN (missing_fields - %s::text[]) = '[]'::jsonb THEN 'under_review' ELSE status END … RETURNING status, missing_fields` (`missing_fields` is `jsonb`; `-` with `text[]` removes those strings). The returned columns are merged into the slice's claim, so no refresh query and no stale overwrite. Then one `audit_events` row whose `detail` holds the received values (there is no documents table). |
| `assess` | code | Wraps a pure `next_actions(claim) -> list[str]` and writes them to the slice. No LLM, no write. |
| `summarise` | LLM | **Kept: the assignment lists "claim summary generation" as a claims capability.** Its prompt is rebuilt from `_claims_facts`-style lines: refs and names, money and day formatted, what was received this turn, what is still missing, and the `next_actions` **as given** (the model phrases the next step, never chooses it). It now runs on every completed path, including after the pause, so the resumed claim is summarised too. **The only node that marks the task `done` and sets `outcome`.** If two nodes own completion, a missed one leaves the task `running` and the supervisor's guard retries it three times, then fails it. |

`next_actions`, from the seeded policy text, each with the citation the chip will show:

| Condition | Next step |
|---|---|
| fields still missing | "Still needed: …. The claim stays Awaiting information and closes automatically after 30 days [claims-handling-policy.md, Missing information]" |
| `amount > 10000` | "Escalate to the duty manager: the claim exceeds 10,000 [operations-runbook.md, Escalation]" |
| `claim_type == "motor"` and `amount >= 5000` | "Book a second review before settlement [claims-handling-policy.md, Motor claims]" |
| `settled` / `rejected` | "None: the claim is closed." |
| otherwise | "Proceed with standard assessment by a single assessor." |

Both threshold rules can apply to one claim (a motor claim of 12,000), hence a list.
Thresholds are module constants. A test reads `data/seed/seed.sql` and asserts the
same numbers appear in the policy text — that test *is* the "RAG and code agree"
claim in `docs/assumptions.md`, made checkable.

**`summarise` stays; compose stops re-describing it.** Deleting it was considered
(one narrator, fewer model calls) and rejected on 2026-09-22 because the assignment
brief lists *claim summary generation* under Claims Operations and the architecture
reference ends the claims subgraph in `generate_summary`. Double narration is fixed
on the compose side instead (Phase 1's pass-through), so the claims workflow now has
two LLM calls, each where ambiguity is: reading a human's free text, and writing the
summary. `summarise` stays in `STREAMING_NODES`; on a single-task turn `compose`
streams nothing because it makes no call.

Outcomes, three: `no_claims`, `summarised`, and `information_incomplete`.
*Deviation from the brainstorm:* it proposed renaming `information_requested` to
`information_received`; with `summarise` as the single owner of the outcome, a
completed claim is simply `summarised` and "received this turn" is a fact line in the
summary, not an outcome.

**A new request typed at the pause is not an answer**, and is still consumed as one
(`docs/tradeoffs.md:122-129`). It extracts nothing, so the pause ends and compose gets
the fact "the reply contained none of the requested details; CLM-5003 is still waiting
for …". The operator is told, not silently ignored. Replies are never stored as
`HumanMessage`, so a restored transcript does not show them; storing them would break
the "last HumanMessage is the request" rule in both compose and the planner, so this
stays a documented gap (`docs/tradeoffs.md`).

The policy sentence "Claims of 5000 or more require a second review" sits under the
*Motor claims* section and follows "Motor claims under 5000…". The code restricts the
rule to motor and the docstring says why. No seeded claim is a motor claim of 5,000 or
more (CLM-5001 is 4,820, deliberately just under), so that rule is covered by unit
tests only — the seed is not changed, because other branches depend on it.

`Reply` is a Pydantic model used only inside `read_reply`; what enters state is a plain
`dict[str, str]`, so nothing is added to `ALLOWED_MSGPACK_MODULES`. Say so in a comment.

Out of scope, written into `docs/future-improvements.md` with the reason (no policies
table): FNOL intake, coverage verification, fraud indicators, and the runbook's "more
than two open claims" escalation (the `get_claim` path does not load sibling claims).

## System-Wide Impact

- **Interaction graph.** `/chat` → `graph.astream` → `translate` → SSE → `Workspace.send`.
  Phase 1 changes the *input* to one model call and adds a field to `token`. Phase 2
  adds one event type end to end. Phase 3 changes one subgraph; the supervisor,
  planner and the other two subgraphs are untouched. `/chat` needs no change: it
  resumes whenever `snapshot.interrupts` is non-empty, which covers a second pause.
- **Error propagation.** A model failure in `read_reply` is caught in the node (see
  the table) and surfaces as an `errors` record plus a fact line. A *process death*
  there is different and already safe: the reply was checkpointed by
  `request_information`, so the resume re-enters at `read_reply` with the operator's
  words intact — durable execution doing its job, worth a line in the demo.
- **State lifecycle.** `record_information` can re-run if the process dies between the
  `UPDATE` and the checkpoint. Removing names that are already gone is a no-op, so the
  `UPDATE` is idempotent; the audit row can duplicate (at-least-once). Accept and say so in the docstring.
  `workflow_states` has no reducer: every node keeps using `_merge_claims_state`.
- **The seed is now mutated by the app.** CLM-5003 stops being incomplete after the
  pause beat. `make seed` truncates and restores it, but it also truncates `knowledge_chunks`,
  so a reseed forces a re-ingest. `docs/demo.md` therefore gets a **one-line reset**
  (a single `UPDATE` putting CLM-5003 back) for repeating the beat, and
  `tests/integration/test_repository.py:67-71` (asserts CLM-5003 is
  `awaiting_information`) becomes order-dependent unless the API integration test
  restores the row in a fixture `finally`.
- **API surface parity.** `GET /sessions/{id}` is unchanged: messages stay `{role, text}`.
- **Integration scenarios** (real Postgres, stub model): (1) CLM-5003, full reply →
  row is `under_review`, `missing_fields == []`, one audit row with the values;
  (2) partial reply → second `interrupt` with a *different id* whose question lists
  only the remaining field, task still `running` (`seen_interrupts` is per stream, so
  the dedupe cannot swallow it); (3) useless reply → no second interrupt, row unchanged,
  outcome `information_incomplete`; (4) kill the process after the first reply, resume
  → `read_reply` runs once, nothing asked twice (`make verify-resume` style);
  (5) a two-task turn where claims pauses → the other task's facts still reach compose.

## Acceptance Criteria

### Phase 1
- [ ] For slices containing UUIDs, chunks and action names, the text handed to the model matches neither a UUID regex nor any `workflow.action` name (`tests/graph/test_orchestration.py`, replacing `:215-244`). *Guard the input, not the output.*
- [ ] `12750.0` → `12,750.00`; `2026-09-01` → `1 Sep 2026`; test passes on Windows.
- [ ] The operator's latest request is in the model input; on a resume turn it is the original request.
- [ ] A failed or skipped task still appears as a plain line with its error.
- [ ] Single knowledge or claims task done this turn: compose makes **zero** model calls (asserted on the stub's call count); `final_response` equals the workflow's `answer` / `summary`, citations byte-identical. A stale text from an earlier turn is ignored.
- [ ] Multi-task turn: each workflow-written text is appended verbatim after the model's text, in plan order.
- [ ] The `NEEDS_INPUT` branch and its prompt rule are gone, after a grep confirms nothing sets that status.
- [ ] `_claims_facts` works on a claim with no `customer_name` (the `get_active_claims` path).
- [ ] `token` frames carry `node`; the frontend draft restarts when it changes.

### Phase 2
- [ ] A mapped node yields one `step` frame with a label; an unmapped node yields none; no frame contains a LangGraph node name.
- [ ] `retrieve` is labelled differently for claims and knowledge (keyed by `ns`).
- [ ] The trail is visible before the first token and survives every token (`pushAssistant` carries `steps`); a paused turn shows a trail-only message ending "needs your answer"; after `restore()` there is none.
- [ ] `request_information` has no step label; the `ns` shape is pinned by a test.
- [ ] `[source, section]` renders as an inline chip, including after `restore()`.
- [ ] `frontend/DESIGN.md` §6 and `PRODUCT.md`'s SSE list mention `step`; `impeccable detect` stays at 0.

### Phase 3
- [ ] A value the model returns that does not occur in the reply is discarded (unit test with a lying stub).
- [ ] Re-ask happens only after progress; a useless reply ends the pause with the "none of the requested details" fact; no counter in state.
- [ ] `request_information` has nothing above `interrupt()` and pauses for `incomplete[0]` only (pinned); the write is in its own node; `summarise` is the only node that sets `done` and `outcome`, and it runs on the resumed path too.
- [ ] The summarise prompt contains the formatted facts and the `next_actions` text verbatim, never a UUID or a raw float (input-side test, like Phase 1).
- [ ] A raising stub model in `read_reply` yields an `errors` record and the "could not be read" fact, not a stuck `running` task.
- [ ] The repository write is one statement; running it twice leaves the same row (`make test-db`).
- [ ] CLM-5003 with both references supplied: row `under_review`, `missing_fields` empty, audit row holds the values, next step names the duty manager with its citation.
- [ ] `next_actions` unit tests cover all five rows, including a motor claim over 10,000 (two actions).
- [ ] A test asserts the thresholds in code appear in the seeded policy text.
- [ ] The `StubModel`s (`test_orchestration.py:48-59`, `test_api.py:40-48`, `test_claims_workflow.py:39`) dispatch `with_structured_output` on the schema — today they hand a `Plan` to any structured call, so `read_reply` would receive a `Plan`. `StubPool` fixtures monkeypatch the new repository function. Tests resume with **text**, as the API does, not a dict.
- [ ] The API integration test restores the CLM-5003 row in a fixture `finally`, so `test_repository.py:67-71` stays order-independent.
- [ ] Docs match the new behaviour: `docs/demo.md:68-71` (+ the reset line), `docs/assumptions.md:28-31`, `docs/langgraph-design.md:123,131` and its claims diagram, `docs/architecture.md:75`, `docs/tradeoffs.md:122-136`, `docs/future-improvements.md`, the claims line in `planner.py:36`, and the claims module docstring.

### Quality gates (every phase)
- [ ] `make test`, `make lint`; after Phase 3 also `make test-db` and **`make verify-resume`**.
- [ ] Every new guard names the failure it prevents; every choice with a competitor records what lost (CLAUDE.md).
- [ ] Net new surface stays small: no new files in `backend/app` or `frontend/components`.

## Implementation notes

Written during implementation (2026-09-22), branch `feat/readable-answers`.

### Phase 0 — the probe did not reach Bedrock

Run inside the backend container as specified. All six calls (three inputs × two
attempts) failed identically:

    LoginRefreshRequired: Your session has expired or credentials have changed.
    Please reauthenticate using 'aws login'.

The host `aws login` session had expired, and re-authenticating is interactive and
would have killed the container's session too (CLAUDE.md: refresh tokens are
single use). So **`Reply` is unproven against Nova Pro** — the schema is the
planner's proven pattern (`with_structured_output(..., include_raw=True)`, a flat
list of pairs) but the extraction quality is a gap to close before the demo.

The probe was not wasted. It failed by **raising out of `ainvoke`**, not by
returning `parsed=None` — which is exactly the case the plan says `include_raw=True`
does not cover. `read_reply`'s `try` wraps the call for that reason, and the
docstring now cites this probe rather than theorising.

### Decisions the plan left open

- **No passage count in the trail.** The plan said to include it only if the
  `retrieve` update visibly carries `chunks`. Probed: the update carries
  `workflow_states` (the whole dict) and `tool_results`, so reading a count would
  mean `streaming.py` reaching inside a workflow's slice — the second mechanism
  the plan said to avoid. Left out.
- **`ns` shape confirmed** as `("claims:<uuid>",)` for subgraph nodes and `()` for
  top-level ones, and pinned by a test.

### Deviations

- **`validate` sets `missing_fields` to `incomplete[0]`'s fields, not the union.**
  The plan kept the union in `validate` and selected the claim in
  `request_information`. Narrowing at the source removes the ambiguous value
  entirely rather than working around it downstream, and `missing_fields` then has
  one meaning everywhere: "outstanding on the claim we are asking about".
- **`record_information` returns early when nothing was received**, instead of
  issuing a no-op `UPDATE` plus an audit row saying nothing arrived. Keeps the
  plan's two-edge diagram out of `read_reply` while keeping the audit log readable.
- **One extra slice key, `supplied_now`.** The router needs to know whether *this*
  reply made progress; `received` accumulates across re-asks and cannot answer it.
- **`summarise` derives the outcome** (`summarised` vs `information_incomplete`)
  from whether any claim still has missing fields. That is how "three outcomes" and
  "one owner of `done`/`outcome`" both hold.
- **`claims_facts` lives in `compose.py` and is imported by `summarise`**, rather
  than the workflow growing its own `_claims_facts`-style formatter. The summary
  and the final answer are then written from the same lines and cannot describe one
  claim differently, and `_money`/`_day` have one home.
- **`onboarding_facts` reads `ineligible_reasons`** as well as `reasons` — that is
  the key the `reject` node actually writes.
- `test_large_plan_does_not_hit_the_recursion_ceiling` now uses CLM-6xxx refs: the
  shared stub makes CLM-5003 incomplete (matching the seed), which would otherwise
  pause that run.
- **`impeccable detect` was not run** — the implementation brief excluded the design
  tooling. `DESIGN.md` §6 and `PRODUCT.md`'s SSE list are updated.

## Dependencies & Risks

| Risk | Mitigation |
|---|---|
| **The working tree is uncommitted.** Almost every file this plan touches (`compose.py`, `streaming.py`, `claims/graph.py`, all of `frontend/components`) is modified or untracked. A git worktree is cut from `HEAD` and would contain none of it. | Commit the current work first (your call), *then* give the implementation agent a worktree. Otherwise run the agent in the main tree with no isolation. |
| Nova Pro paraphrases citations or drops "Next step" | Pass-through for knowledge; "copy anything in [square brackets] exactly"; assert on the input. Tune the prompt on the probe, not on hope. |
| Nova Pro structured output flakes on `Reply` | Phase 0 probe, twice; `include_raw=True` so a bad parse is "nothing supplied", never an exception. |
| A looped node with `interrupt()` misbehaves on resume | Integration scenario 2 and `make verify-resume`; the stream dedupes interrupts by id (`streaming.py:95`), and a second pause has a new id. |
| The CLM-5003 beat works once per seed | One-line reset `UPDATE` in `docs/demo.md`; a full `make seed` also needs a re-ingest. |
| Verification guard reads as stronger than it is | Docstring: it stops invented values, it does not validate a reference format. |
| Host `aws` tooling kills the container's session | Probe from inside the container only. |

## Sources & References

- **Origin brainstorm:** [docs/brainstorms/2026-09-21-operator-answers-and-claims-realism-brainstorm.md](../brainstorms/2026-09-21-operator-answers-and-claims-realism-brainstorm.md).
  Carried forward: LLM for language and code for decisions; the composer reports and
  never decides (next step computed upstream); rules come from the seeded policy text;
  the write is its own node after the interrupt; the pause question stays code-written;
  process out of the prose by construction; the trail is browser-observed; sources are
  chips parsed from text; no invented currency; the `information_received` rename (superseded in Phase 3: `summarise`
  owns the outcome, so a completed claim is `summarised`); guard
  the input, not the output; FNOL / coverage / fraud out of scope.
- Brainstorm open questions, resolved here: extraction uses the planner's
  `with_structured_output(include_raw=True)` pattern with a list-of-pairs schema (probe
  first); knowledge answers are never paraphrased (pass-through when alone, appended
  verbatim otherwise); trail shows a mapped subset of
  nodes; re-asking is bounded by progress, not a counter. Still open: passage count in
  the trail (decide from what the `retrieve` update carries).
- Code: `backend/app/graph/compose.py:25-64`, `backend/app/api/streaming.py:33,80-136`,
  `backend/app/api/chat.py:57-61,80-90`, `backend/app/graph/workflows/claims/graph.py:157-192,233-260`,
  `backend/app/graph/workflows/knowledge/graph.py:122-149`, `backend/app/graph/planner.py:103-132`,
  `backend/app/db/repository.py` (`get_claim`, `record_audit_event`, `_serialise`),
  `data/seed/seed.sql:22-34,45-65`, `frontend/components/ChatPanel.tsx` (`RECORD_ID`, `withRecordIds`),
  `frontend/components/Workspace.tsx::send`.
- Spec-flow analysis (2026-09-21) added: the caught `read_reply` failure, the
  single-owner rule for `done` (now `summarise`), `incomplete[0]`, the one-statement idempotent `UPDATE`,
  the trail-only paused turn, the stub dispatch, and the dead `NEEDS_INPUT` branch.
- Tests that change: `tests/graph/test_orchestration.py:35-59,215-244`,
  `tests/graph/test_claims_workflow.py:39,120-150,163-184`,
  `tests/integration/test_api.py:33,161-176,196`, `tests/integration/test_repository.py:67-71`.
- Gotchas relied on (CLAUDE.md): `interrupt()` re-runs its node; interrupts arrive on
  `updates` keyed `__interrupt__`; custom types in state need msgpack registration;
  repository functions return plain dicts; seed fixtures are load-bearing; `aws login`
  tokens are single use.
