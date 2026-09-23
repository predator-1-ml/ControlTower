---
date: 2026-09-21
topic: operator-answers-and-claims-realism
---

# Answers a handler can read, and a claims workflow that does something

## What We're Building

Three changes, in this order.

**1. Answers that read like a colleague wrote them.** Today the final answer is a
state dump: "The task claims.retrieve_claims completed successfully… claim ID is
44d65b54-…, amount is 12750.0". From the frontend it feels like a deterministic
system, not an assistant. The target: the answer responds to what the operator
actually asked, in plain business language, as a short brief — one outcome sentence,
the two to four facts that matter, then a `Next step:` line. Business references
only (CLM-5003, Tom Baker, 12,750.00, 1 Sep 2026); never task names, UUIDs or raw
floats. Plain text with line breaks, so the message shape on the wire and in the
checkpoint does not change. The brief is a
guide, not a template: "Does CUST-1001 have an active claim?" opens with "Yes, one".

**2. What the system did is shown, not narrated.** "Task [t1]
knowledge.answer_question completed successfully… Relevant citations were provided"
is process, and process does not belong in the message. Every assistant turn gets a
compact **step trail** above the answer that fills in live as the graph runs
("Planned 1 task › Knowledge: searched policy documents › 2 passages found ›
answered") and then stays as a quiet one-liner. The answer below it is only the
answer, with its sources as chips. This is also what makes the conversation feel like
an assistant working rather than a form returning a result.

**3. A claims workflow where the pause matters.** Today the operator can type
anything at the pause and it "completes": the reply is echoed, the claim row never
changes, no audit row is written. The target: the LLM reads the free-text reply and
extracts what was actually supplied; if something is still missing it asks again for
just that; once complete the claim is updated (missing fields cleared, status moves
to Under review) with an audit row; and a small pure function applies the handling
rules to produce the next step.

## Why This Approach

Root causes (research, 2026-09-21): `compose.py::_summarise` hands the model a raw
`str(dict)` of workflow state with an exclude-list that misses the keys carrying
UUIDs; task lines print `workflow.action`; the prompt says "report only what the
results state" and never names the reader or asks for a next step. So the fix is at
the source — what the model is given — not only in the prompt.

| Considered | Verdict |
|---|---|
| Curated allow-list of fields + audience prompt, short brief in plain text | **Chosen.** Removes UUIDs and noise before the model sees them; no frontend change. |
| Model returns JSON rendered as a record card | Rejected: response schema, structured output on Nova Pro, new SSE payload and a new component to defend. |
| Cleaned-up paragraph | Rejected: a paragraph is the hardest shape to scan, which was the complaint. |
| Process view: a live step trail on each answer | **Chosen.** One small SSE event per graph node (node names are already on the `updates` stream the API reads) and one small component. |
| Process view: nothing new, the Plan panel is the state machine | Rejected: the conversation still shows no sign of the system working. |
| Process view: node-level rows inside the Plan panel | Rejected: per-workflow node lists to keep in sync with the frontend, and a busier panel. |
| Deterministic templates, no LLM in compose | Rejected: that is exactly the "feels deterministic" problem. |
| Claims: real rules but accept any reply | Rejected: "type anything and it completes" is the least realistic beat in the demo. |
| Claims: add a policies table and coverage check | Rejected for now: new migration and seed rows other tests depend on. Goes to `future-improvements.md` with the reason. |

## Key Decisions

- **LLM for language, code for decisions.** The model reads the operator's reply and
  writes the answer; thresholds and status transitions are plain code. Same split
  onboarding already defends ("no LLM where a decision has legal weight").
- **The composer still reports and never decides** (`compose.py` docstring). The next
  step is computed upstream by a pure `next_action(claim)` and handed to compose as a
  fact; compose may phrase it, never invent it.
- **The rules come from the seeded policy text**, so RAG and code finally agree: motor
  claim of 5,000 or more → second review; any claim over 10,000 → duty manager. This
  makes `docs/assumptions.md:28` true (it currently is not). CLM-5003 at 12,750
  already exercises the escalation rule; no seed change is needed for it.
- **Persisting the reply is its own node after the interrupt**, never in the
  interrupting node: `interrupt()` re-runs its node from the top on resume, so a write
  or an LLM call placed before it runs twice (CLAUDE.md).
- **The pause question stays code-written** for the same reason — an LLM call before
  `interrupt()` would run twice and could word the question differently on resume.
- **Process out of the prose, by construction.** Compose is never given task ids or
  `workflow.action` names, so it cannot narrate them; the trail carries them instead.
- **The trail is what this browser observed**, like the activity log: built from
  streamed events, not replayed after a restore. The durable record stays
  `audit_events`. Step labels are operator words ("searched policy documents"), mapped
  in one place; an unmapped node shows nothing rather than a raw node name.
- **Sources are chips, parsed from the answer text.** Citations already arrive inline
  as `[source, section]`, built from the retrieved chunks rather than from the model.
  Parsing them in the frontend (as record ids already are) survives a restore, which a
  separate sources payload would not without changing the stored message shape.
- **No invented currency.** There is no currency column; amounts are formatted
  (12,750.00) but not labelled.
- **Rename the outcome** `information_requested` → `information_received`: after the
  resume, the information was received.
- **Guard the input, not the output.** Tests assert that what is handed to the model
  contains no UUID and no `workflow.action` name. Asserting on LLM prose would be
  flaky; asserting on the curated input is deterministic.
- **Out of scope, written down:** FNOL intake, coverage verification, fraud
  indicators — no policies table, and each is a new thing to defend.

## Open Questions

- Extraction call: structured output on Nova Pro, or a tolerant plain-text parse? Needs
  a two-call probe (CLAUDE.md: never trust one green call).
- When a workflow's own `summary` / `answer` was the turn's only task, should compose
  pass it through untouched? Today it streams once from its node and is then
  paraphrased again by compose (double narration).
- Step trail granularity: every node, or only the ones a handler would recognise
  (plan, search, check, ask, answer)? Leaning to the mapped subset.
- Does the trail need the passage count ("2 passages found")? That is data on the
  node's update, not just its name — check what `streaming.py` already sees.
- How many times may the claims pause re-ask before it gives up and records the claim
  as still incomplete?
- Tests to update: `test_orchestration.py:215-244` (summary line format),
  `test_claims_workflow.py:163-184` (outcome name; resumes with a dict the API never
  sends), `integration/test_api.py:171-176`. `StubPool` has no methods, so the new
  repository write needs a monkeypatch in both stub fixtures.
- `docs/demo.md:68-71` says "Supply anything; it completes" — the script changes with
  the behaviour.

## Next Steps

→ `/ce:plan` for implementation details, then a separate implementation agent on its
own branch. After touching `graph/`: `make verify-resume`.
