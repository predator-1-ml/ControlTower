"""Assemble the main graph.

    START -> planner -> supervisor -+-> onboarding -+
                                    +-> claims     -+-> supervisor (loop)
                                    +-> knowledge  -+
                                    +-> compose -> END

Subgraphs are added as compiled graphs, directly. They share ControlTowerState's
channel names, so no wrapper is needed to translate inputs — and because no
checkpointer is passed to them, they inherit the parent's, which is what lets an
`interrupt()` deep inside a workflow propagate all the way to the top.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graph.compose import compose_response
from app.graph.deps import Deps
from app.graph.planner import plan_node
from app.graph.state import ControlTowerState
from app.graph.supervisor import supervise, unimplemented_workflow
from app.graph.workflows.claims.graph import build_claims_graph
from app.graph.workflows.onboarding.graph import build_onboarding_graph

#: A supervisor loop costs 2 super-steps per task (supervisor -> workflow ->
#: supervisor), plus the skip/retry passes. LangGraph's default recursion_limit
#: is 25, which dies at roughly 11 tasks with GraphRecursionError — a cliff a
#: 3-task demo never reaches and a real plan does. Pass this in the invoke config.
RECURSION_LIMIT = 100


def build_graph(checkpointer=None, *, include_knowledge: bool = False):
    """Compile the control tower graph.

    `include_knowledge` is False until the RAG workflow lands; until then a
    knowledge task fails with a clear reason rather than hanging the run.
    """
    builder = (
        StateGraph(ControlTowerState, context_schema=Deps)
        .add_node("planner", plan_node)
        # The supervisor routes with Command(goto=...), so its edges are inferred
        # from its Destination Literal rather than declared here.
        .add_node("supervisor", supervise)
        .add_node("onboarding", build_onboarding_graph())
        .add_node("claims", build_claims_graph())
        .add_node("compose", compose_response)
        .add_edge(START, "planner")
        .add_edge("planner", "supervisor")
        # Every workflow returns to the supervisor, which decides what is next.
        # This is the loop that makes multi-task plans work.
        .add_edge("onboarding", "supervisor")
        .add_edge("claims", "supervisor")
        .add_edge("compose", END)
    )

    if include_knowledge:
        from app.graph.workflows.knowledge.graph import build_knowledge_graph

        builder = builder.add_node("knowledge", build_knowledge_graph())
    else:
        builder = builder.add_node("knowledge", unimplemented_workflow)
    builder = builder.add_edge("knowledge", "supervisor")

    return builder.compile(checkpointer=checkpointer)
