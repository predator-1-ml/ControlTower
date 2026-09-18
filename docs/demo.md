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

> **"Onboard CUST-1002 and check whether they already have an active claim."**

This is the assignment's own worked example, verbatim.

**What to point at:** the plan appears in the right-hand panel **before any task
starts running**, with `t2` showing `waits for t1`. The claims lookup depends on
the onboarding task because it needs the customer identified first.

That ordering is not cosmetic — the backend emits the `plan` SSE event the moment
the planner returns, before dispatching any work. Planning before execution is
visible, not just true.

Tasks then move `pending → running → done` in dependency order.

---

## Beat 2 — Context across a workflow switch

Continue in the same session:

> **"Actually, summarise claim CLM-5003 first."**

**What to point at:** the plan *extends*. The onboarding task is still there, in
its previous state. A third task appears for claims.

Then:

> **"Now go back to the onboarding."**

The supervisor picks the original task up again.

**Why this works:** the planner appends rather than replaces, so nothing is
discarded, and `workflow_states` is keyed per workflow so each keeps its own
context. There is no workflow-switch branch anywhere in the code — it falls out of
the design.

CLM-5003 is missing `incident_report` and `police_reference`, so this beat also
pauses and asks for them. Supply anything; the workflow completes.

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
