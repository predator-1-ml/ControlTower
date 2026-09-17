"""Prove durable execution across process death. Run as two separate processes.

    python -m app.scripts.verify_resume start  --thread demo
    #  ^ pauses at an interrupt, then EXITS — the process is gone
    python -m app.scripts.verify_resume resume --thread demo

If the second command completes the workflow, state genuinely lives in Postgres
rather than in memory, and an ECS task can be replaced mid-workflow without
losing work. That is the claim the architecture rests on.

The unit tests use an in-memory checkpointer, which cannot fail the way a
redeployed container can. This is the check that can.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from langgraph.types import Command

from app.core.eventloop import use_compatible_event_loop
from app.db.checkpointer import build_checkpointer, open_pool
from app.graph.smoke import build_smoke_graph

EXPECTED = ["stage_one", "ask_human", "stage_two"]


async def _run(mode: str, thread: str, answer: str) -> int:
    pool = await open_pool()
    try:
        graph = build_smoke_graph(build_checkpointer(pool))
        config = {"configurable": {"thread_id": thread}}

        if mode == "start":
            result = await graph.ainvoke({"steps": [], "answer": None}, config)
            if not result.get("__interrupt__"):
                print(f"FAIL: expected a pause, got {result}")
                return 1
            print(f"PAUSED at interrupt, thread={thread!r}, steps={result['steps']}")
            print("State is now only in Postgres. This process is exiting.")
            return 0

        # Resume: a new process, sharing nothing with the first but the database.
        result = await graph.ainvoke(Command(resume=answer), config)
        if result["steps"] != EXPECTED or result["answer"] != answer:
            print(f"FAIL: steps={result['steps']} answer={result['answer']!r}")
            return 1

        print(f"RESUMED AND COMPLETED: steps={result['steps']}")
        print("stage_one ran in a process that no longer exists.")
        return 0
    finally:
        await pool.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["start", "resume"])
    parser.add_argument("--thread", default="verify-resume")
    parser.add_argument("--answer", default="resumed-from-postgres")
    args = parser.parse_args()
    use_compatible_event_loop()  # local Windows dev; no-op on Linux
    return asyncio.run(_run(args.mode, args.thread, args.answer))


if __name__ == "__main__":
    sys.exit(main())
