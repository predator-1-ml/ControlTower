"""Knowledge Operations workflow — retrieval-augmented answering with citations.

    embed -> retrieve -> generate    (or -> no_results)

The third of the three patterns: onboarding is deterministic rules, claims is
tool-driven, this one is retrieval. Together they cover the distinct shapes the
assignment asks for.

**No query-rewrite node, deliberately.** The HLD sketched one, and it is standard
in production RAG where user queries are terse and the corpus is large. Against a
five-document policy corpus it is a round trip and a token bill that changes
nothing measurable. Noted in the docs as the first thing to add when the corpus
grows — but adding it now would be ceremony.

The prompt is the guardrail that matters here: the model may answer only from
retrieved text, and must say so when the text does not cover the question. An
operations assistant that fluently invents policy is worse than one that declines.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.db import repository
from app.graph.deps import Deps
from app.graph.state import ControlTowerState, TaskStatus, running_task, task_delta
from app.llm.provider import message_text

WORKFLOW = "knowledge"
TOP_K = 4

SYSTEM_PROMPT = """You answer operational policy questions for an insurance handler.

Answer ONLY from the excerpts provided. Rules:
- Answer the question in the first sentence. Then any detail that changes what
  the handler does.
- If the excerpts do not answer the question, say so plainly. Do not fill the gap
  from general knowledge — a confident invented policy is worse than no answer.
- Cite the source of each claim inline, as [source, section].
- Be brief. Two or three sentences unless the question genuinely needs more.
- Plain text. Short lines. No headings, no preamble."""
# This answer is never paraphrased downstream: when it is the turn's only task it
# IS the final response (compose.py). So the voice rules have to live here too —
# there is no second pass to tidy it up.


def _ws(state: ControlTowerState) -> dict[str, Any]:
    return state.get("workflow_states", {}).get(WORKFLOW, {})


def _merge(state: ControlTowerState, **changes: Any) -> dict[str, Any]:
    all_states = dict(state.get("workflow_states", {}))
    all_states[WORKFLOW] = {**_ws(state), **changes}
    return all_states


def _question(state: ControlTowerState) -> str:
    """The task's explicit question, else the latest human message."""
    task = running_task(state["plan"], WORKFLOW)
    if task and task.args.get("question"):
        return str(task.args["question"])
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


async def embed(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    question = _question(state)
    embedder = runtime.context.embedder

    if embedder is None or not question:
        # Missing embedder is a configuration problem, not a user-facing failure
        # mode worth special nodes. Record it and let retrieve find nothing.
        return {"workflow_states": _merge(state, question=question, vector=None)}

    vector = await embedder.aembed_query(question)
    return {"workflow_states": _merge(state, question=question, vector=vector)}


async def retrieve(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    vector = _ws(state).get("vector")
    model = runtime.context.embedding_model
    chunks = (
        await repository.search_knowledge(runtime.context.pool, vector, model, limit=TOP_K)
        if vector and model
        else []
    )

    await repository.record_audit_event(
        runtime.context.pool,
        trace_id=state["trace_id"],
        session_id=state["session_id"],
        workflow=WORKFLOW,
        node="retrieve",
        status="ok",
        detail={"retrieved": len(chunks)},
    )
    # Drop the vector: it is 1024 floats that would otherwise be written into
    # every checkpoint from here on for no reason.
    return {"workflow_states": _merge(state, chunks=chunks, vector=None)}


def route_after_retrieve(state: ControlTowerState) -> Literal["generate", "no_results"]:
    return "generate" if _ws(state).get("chunks") else "no_results"


async def no_results(state: ControlTowerState) -> dict[str, Any]:
    task = running_task(state["plan"], WORKFLOW)
    if task is None:
        return {}
    return {
        "plan": task_delta(task, status=TaskStatus.DONE, result_ref="no_matching_policy"),
        "workflow_states": _merge(
            state,
            outcome="no_matching_policy",
            answer="No internal policy document covers that question.",
            citations=[],
        ),
    }


async def generate(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    task = running_task(state["plan"], WORKFLOW)
    if task is None:
        return {}

    chunks = _ws(state).get("chunks", [])
    excerpts = "\n\n".join(
        f"[{c['source']}, {c.get('section') or 'general'}]\n{c['content']}" for c in chunks
    )

    prompt = f"Question: {_ws(state).get('question')}\n\nExcerpts:\n{excerpts}"
    response = await runtime.context.model.ainvoke(
        [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]
    )
    answer = message_text(response)

    # Citations come from what was actually retrieved, never from the model's
    # output. A model asked to list its sources will occasionally list one it did
    # not use, and a fabricated citation is worse than none.
    citations = [
        {"source": c["source"], "section": c.get("section"), "similarity": c.get("similarity")}
        for c in chunks
    ]

    return {
        "plan": task_delta(task, status=TaskStatus.DONE, result_ref="answered"),
        "workflow_states": _merge(state, outcome="answered", answer=answer, citations=citations),
    }


def build_knowledge_graph(checkpointer=None):
    return (
        StateGraph(ControlTowerState, context_schema=Deps)
        .add_node("embed", embed)
        .add_node("retrieve", retrieve)
        .add_node("generate", generate)
        .add_node("no_results", no_results)
        .add_edge(START, "embed")
        .add_edge("embed", "retrieve")
        .add_conditional_edges("retrieve", route_after_retrieve, ["generate", "no_results"])
        .add_edge("generate", END)
        .add_edge("no_results", END)
        .compile(checkpointer=checkpointer)
    )
