"""Event-loop compatibility shim for local Windows development.

psycopg's async mode cannot run on Windows' default `ProactorEventLoop`:

    Psycopg cannot use the 'ProactorEventLoop' to run in async mode.

The failure is unhelpfully indirect. The pool does not raise — it retries the
connection in a loop, logging a warning each time, and eventually surfaces as
`PoolTimeout: pool initialization incomplete after 10 sec`, which reads like the
database is unreachable. You will go and check Postgres first. It's fine.

This is **local-dev only**. ECS runs Linux, where the default selector loop is
already compatible, so this is a no-op in every deployed environment. It is kept
as an explicit call rather than an import side effect so that the one place it
matters is obvious and greppable.
"""

from __future__ import annotations

import asyncio
import sys


def use_compatible_event_loop() -> None:
    """Select an event loop psycopg can use. No-op off Windows.

    Must be called BEFORE the loop is created — i.e. before `asyncio.run()`.

    ⚠️ **This does not reach uvicorn.** Uvicorn does not consult the event-loop
    policy; it returns a loop *factory* directly::

        # uvicorn/loops/asyncio.py
        if sys.platform == "win32" and not use_subprocess:
            return asyncio.ProactorEventLoop

    So a native `uvicorn app.main:app` on Windows always gets ProactorEventLoop
    and dies at startup with `PoolTimeout`, no matter what is set here.

    Note the inverse in that condition: with a subprocess it returns
    SelectorEventLoop — so **`uvicorn --reload` works**, which is what local dev
    wants anyway (`make dev`). Irrelevant in the container and on ECS, where
    SelectorEventLoop is already the default.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
