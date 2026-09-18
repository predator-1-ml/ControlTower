"""Knowledge workflow: retrieval, citations, and the no-match path.

A deterministic fake embedder keeps this free of AWS. What is under test is the
plumbing and the guardrails, not retrieval quality — the interesting assertions
are that citations come from retrieved chunks rather than the model, and that a
missing embedder degrades instead of exploding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.db import repository
from app.graph.deps import Deps
from app.graph.state import PlanTask, TaskStatus, new_state
from app.graph.workflows.knowledge.graph import build_knowledge_graph


class StubPool:
    """No methods: audit writes fail and must be swallowed."""


@dataclass
class StubResponse:
    content: str


class StubModel:
    def __init__(self, reply: str = "Claims over 5000 need a second review.") -> None:
        self.reply = reply
        self.calls: list[str] = []

    async def ainvoke(self, messages, *_a: Any, **_kw: Any) -> StubResponse:
        self.calls.append(str(messages[-1].content))
        return StubResponse(self.reply)


class FakeEmbedder:
    """Deterministic 1024-dim vector. Never called for content, only shape."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def aembed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [0.01] * 1024


CHUNKS = [
    {
        "source": "claims-handling-policy.md",
        "section": "Motor claims",
        "content": "Claims of 5000 or more require a second review before settlement.",
        "similarity": 0.83,
    },
    {
        "source": "operations-runbook.md",
        "section": "Escalation",
        "content": "Escalate to the duty manager when a claim exceeds 10000.",
        "similarity": 0.71,
    },
]


@pytest.fixture
def search_returns(monkeypatch):
    def _set(chunks: list[dict]) -> None:
        async def fake_search(_pool, _vector, _model, limit=4):
            return chunks[:limit]

        monkeypatch.setattr(repository, "search_knowledge", fake_search)

    return _set


def make_state(question: str | None = None):
    state = new_state(session_id="s1", user_id="u1", trace_id="t1")
    state["plan"] = [
        PlanTask(
            id="1",
            workflow="knowledge",
            action="answer_question",
            args={"question": question} if question else {},
            status=TaskStatus.RUNNING,
        )
    ]
    return state


def deps(model: StubModel | None = None, embedder: Any = "default") -> Deps:
    resolved = FakeEmbedder() if embedder == "default" else embedder
    return Deps(
        pool=StubPool(),
        model=model or StubModel(),
        embedder=resolved,
        # Tracks the embedder: `retrieve` requires both, so leaving this set while
        # the embedder is None would test a state the app cannot produce.
        embedding_model="fake-1024" if resolved else None,
    )


async def test_answers_from_retrieved_excerpts(search_returns):
    search_returns(CHUNKS)
    model = StubModel()
    graph = build_knowledge_graph()

    result = await graph.ainvoke(
        make_state("When does a claim need a second review?"), context=deps(model)
    )

    ws = result["workflow_states"]["knowledge"]
    assert ws["outcome"] == "answered"
    assert result["plan"][0].status is TaskStatus.DONE
    # The retrieved text must reach the prompt, or the model is inventing policy.
    assert "second review before settlement" in model.calls[0]


async def test_citations_come_from_retrieval_not_the_model(search_returns):
    """A model asked to list its sources will sometimes list one it did not use.

    Citations are therefore built from the retrieved chunks. A fabricated
    citation in an operations tool is worse than no citation at all.
    """
    search_returns(CHUNKS)
    model = StubModel(reply="Some answer citing [invented-document.md, Nowhere].")
    graph = build_knowledge_graph()

    result = await graph.ainvoke(make_state("anything"), context=deps(model))

    sources = [c["source"] for c in result["workflow_states"]["knowledge"]["citations"]]
    assert sources == ["claims-handling-policy.md", "operations-runbook.md"]
    assert "invented-document.md" not in sources


async def test_no_matching_policy_is_a_clean_outcome(search_returns):
    search_returns([])
    model = StubModel()
    graph = build_knowledge_graph()

    result = await graph.ainvoke(
        make_state("what is the refund policy for yachts"), context=deps(model)
    )

    ws = result["workflow_states"]["knowledge"]
    assert ws["outcome"] == "no_matching_policy"
    assert ws["citations"] == []
    assert result["plan"][0].status is TaskStatus.DONE
    # The model must not be asked to answer with nothing to answer from.
    assert model.calls == []


async def test_missing_embedder_degrades_rather_than_raising(search_returns):
    """Embeddings are optional config; the other two workflows must not need them."""
    search_returns(CHUNKS)
    graph = build_knowledge_graph()

    result = await graph.ainvoke(make_state("a question"), context=deps(embedder=None))

    assert result["workflow_states"]["knowledge"]["outcome"] == "no_matching_policy"


async def test_query_vector_is_not_kept_in_state(search_returns):
    """1024 floats would otherwise be written into every later checkpoint."""
    search_returns(CHUNKS)
    graph = build_knowledge_graph()

    result = await graph.ainvoke(make_state("a question"), context=deps())

    assert result["workflow_states"]["knowledge"]["vector"] is None


async def test_falls_back_to_the_latest_user_message(search_returns):
    """A planner that omits `question` in args must not break the workflow."""
    from langchain_core.messages import HumanMessage

    search_returns(CHUNKS)
    embedder = FakeEmbedder()
    state = make_state()  # no question in args
    state["messages"] = [HumanMessage(content="what is the escalation threshold")]
    graph = build_knowledge_graph()

    await graph.ainvoke(state, context=deps(embedder=embedder))

    assert embedder.queries == ["what is the escalation threshold"]
