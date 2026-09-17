"""Runtime dependencies handed to graph nodes.

LangGraph distinguishes two things a node can read:

  * **state**  — per-thread, checkpointed, evolves as the run proceeds
  * **context** — per-invocation, NOT checkpointed, fixed for the run

A connection pool and a model client belong in context: they are live objects
that must never be serialised into a checkpoint, and they are identical for
every node in a run. Putting them in state would fail at the first checkpoint
write.

Injecting them (rather than importing a module-level singleton) is also what
makes the workflow subgraphs testable without a database or an API key — a test
passes a stub pool and a fake model.
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from psycopg_pool import AsyncConnectionPool


@dataclass
class Deps:
    pool: AsyncConnectionPool
    model: BaseChatModel
