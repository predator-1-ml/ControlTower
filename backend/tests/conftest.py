"""Shared pytest configuration.

pytest-asyncio creates its own event loop, so the shim that `app.main` and the
scripts apply at import does not reach it. Setting the policy here, at collection
time, covers every async test.

Without this, DB-backed tests on Windows fail as `PoolTimeout` — which reads as
"Postgres is down" rather than "wrong event loop".
"""

from __future__ import annotations

from app.core.eventloop import use_compatible_event_loop

use_compatible_event_loop()
