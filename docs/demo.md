# Demo Runbook

Three beats, in this order. The third is the one that matters.

## Setup

```bash
make up && make migrate && make seed
make ingest                 # embeds the policy corpus — see the warning below
make dev                    # backend on :8000
cd frontend && npm run dev   # frontend on :3000
```

Requires either `ANTHROPIC_API_KEY` in `backend/.env`, or `LLM_PROVIDER=bedrock`
with a current `aws login` session.

**Run `make ingest` before the demo, and check `aws login` has not expired.**
Skipping it produces no error: retrieval simply finds nothing and beat 3 answers
"No internal policy document covers that question", which reads as a broken demo
rather than an unembedded corpus.

---

## Beat 1 — Planning before execution

> **"Onboard CUST-1001 and check whether they already have an active claim."**

This is the assignment's own worked example, verbatim.

**What to point at:** the plan appears in the right-hand panel **before any task
starts running**, with `t2` showing `waits for t1`. Above it, the **Session** card
already lists the three workflows; two of them fill in as the turn ends
(Onboarding: manual review, Claims: CLM-5001 open) and Knowledge stays "Not used
yet". The answer's step trail opens with `● Onboarding ● Claims` — the handoff is
in the transcript, not only the panel. The claims lookup depends on
the onboarding task because it needs the customer identified first.

That ordering is not cosmetic — the backend emits the `plan` SSE event the moment
the planner returns, before dispatching any work. Planning before execution is
visible, not just true.

Tasks then move `pending → running → done` in dependency order.

CUST-1001 is verified but has an open motor claim (CLM-5001), so onboarding ends
in **manual review** — the seeded eligibility policy, applied in code. That is an
outcome, not a failure: the task is `done`. CUST-1001 is used here rather than
CUST-1002 because CUST-1002 pauses for documents, and a paused session treats the
next message as the answer (Beat 2's limit) — it is saved for Beat 3.

---

## Beat 2 — Context across a workflow switch

Continue in the **same session**. Do not name the customer:

> **"Do they have any other open claims?"**

**What to point at:** the plan *extends* — `t1` and `t2` are still there, done, and
a `t3` appears for claims. The claims workflow was never told who "they" is. It
reads `customer_id`, which the onboarding task published to shared state a turn
ago. That is context crossing both a turn boundary and a workflow boundary — and
the Session card's first line, `Customer CUST-1001 Priya Raman`, is where the
audience sees the context the workflow used. This turn's trail carries only a
`● Claims` chip: one workflow, picked by the planner.

Then switch to a third workflow:

> **"When does a motor claim need a second review?"**

`t4` is a knowledge task, answered from the policy documents with citations. The
onboarding and claims panels keep what they showed before.

Then a workflow that pauses:

> **"Summarise claim CLM-5003."**

CLM-5003 is missing `incident_report` and `police_reference`, so the claims
workflow stops and asks for them. Answer in plain words:

> **"Incident report IR-2291, police ref PR-77431"**

**What to point at:** the reply is *read*, not echoed. The model extracts the two
references, code checks each value actually occurs in what was typed (so an
invented reference is discarded), one node writes them, and the claim row moves to
`under_review` with an `audit_events` row holding the values. The answer ends with
a next step the code chose from the seeded policy — escalation to the duty
manager, because 12,750 exceeds 10,000 — with the citation beside it.

Supply only one of the two ("the police reference is PR-77431") and it asks again
for just the other. Supply neither and the pause ends honestly: the claim is
reported as still waiting, not quietly completed.

> **Repeating this beat:** it mutates the seed. Put CLM-5003 back with
> ```sql
> UPDATE claims SET status = 'awaiting_information',
>   missing_fields = '["incident_report", "police_reference"]'::jsonb
>  WHERE claim_ref = 'CLM-5003';
> ```
> `make seed` also works, but it truncates `knowledge_chunks` too, so it forces a
> re-ingest before Beat 1's knowledge question works again.

**Why this works:** three things persist in the checkpoint between turns — the
plan (the planner appends, it never replaces), `workflow_states` (keyed per
workflow, so each keeps its own context), and `customer_id`. There is no
workflow-switch branch anywhere in the code.

**The limit, stated up front:** while a workflow is paused on a question, the next
message *is* the answer — it is delivered as `Command(resume=...)`, not planned.
You cannot park a question, do something else, and come back. See
`docs/tradeoffs.md`.

---

## Beat 3 — Surviving a crash mid-workflow

**This is the beat that separates the system from a chatbot.** Do not cut it.

1. Start a workflow that pauses:
   > **"Onboard CUST-1002"**

   CUST-1002 is `unverified`, so onboarding stops at `request_documents` and asks
   for a photo ID and proof of address.

2. **Kill the backend.** Ctrl-C the `make dev` process, or `docker kill` the
   container. On AWS: force a new deployment, or stop the ECS task.

3. **Start it again.** The process that ran `load_customer` and `verify_identity`
   no longer exists.

4. Refresh the page and answer the question. **The workflow resumes from exactly
   where it stopped** and runs to completion.

**What to say:** state lives in Postgres, not in the process. That is what makes
ECS tasks disposable — and it is why the architecture checkpoints rather than
trying to keep tasks alive long enough to finish. `stopTimeout` caps at 120s on
Fargate; a long LangGraph run will be killed mid-flight eventually, so the design
assumes it.

The same property is verifiable without the UI:

```bash
make verify-resume
```

```
PAUSED at interrupt, thread='...', steps=['stage_one']
State is now only in Postgres. This process is exiting.

RESUMED AND COMPLETED: steps=['stage_one', 'ask_human', 'stage_two']
stage_one ran in a process that no longer exists.
```

---

## Optional beat — RAG with citations

> **"When does a claim need a second review?"**

Answers from the seeded policy documents with `[source, section]` citations.

**Worth mentioning:** citations are built from the retrieved chunks, never parsed
from the model's output. A test proves it by having the model cite a document that
does not exist and asserting it never reaches the citation list. A fabricated
citation in an operations tool is worse than no citation.

---

## If something fails mid-demo

Every SSE frame carries a `trace_id`, shown at the bottom of the right-hand panel.

```sql
SELECT node, status, latency_ms, detail
FROM audit_events WHERE trace_id = '<the id on screen>'
ORDER BY created_at;
```

Being able to do that live is a better answer than the failure not happening.

---

## Recording

3–5 minutes. Beats 1–3 in order, one take if possible.

Afterwards:

```bash
cd terraform/environments/dev && terraform destroy
```

Roughly $4.40/day in ap-southeast-1 if it stays up.
