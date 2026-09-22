"""How the pool finds the database: a URL locally, Secrets Manager on ECS.

This path cannot run locally or in CI — there is no RDS secret to read — so
without a test it would first execute in production. Terraform passes DB_HOST /
DB_SECRET_ARN and no DATABASE_URL; before this existed the app ignored them and
dialled localhost:5432 inside the task.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from psycopg.conninfo import conninfo_to_dict

from app.core.config import Settings
from app.db.checkpointer import conninfo


def test_without_a_secret_arn_the_url_is_used_as_is():
    settings = Settings(database_url="postgresql://u:p@localhost:5432/db", db_secret_arn=None)
    assert conninfo(settings) == "postgresql://u:p@localhost:5432/db"


async def test_with_a_secret_arn_credentials_are_read_per_connection(monkeypatch):
    """The callable is what makes rotation a non-event: the pool calls it for
    every connection it opens, so the second call must see the rotated password
    rather than a cached one."""
    passwords = iter(["before-rotation", "after-rotation"])

    class FakeSecretsManager:
        def get_secret_value(self, SecretId):  # noqa: N803 - boto3's casing
            assert SecretId == "arn:aws:secretsmanager:::secret:db"
            # The shape RDS writes for a managed master password.
            return {"SecretString": json.dumps({"username": "ct", "password": next(passwords)})}

    monkeypatch.setitem(
        sys.modules, "boto3", SimpleNamespace(client=lambda *_a, **_kw: FakeSecretsManager())
    )

    fetch = conninfo(
        Settings(
            db_host="db.internal",
            db_name="control_tower",
            db_secret_arn="arn:aws:secretsmanager:::secret:db",
        )
    )

    first = conninfo_to_dict(await fetch())
    second = conninfo_to_dict(await fetch())

    assert first["host"] == "db.internal"
    assert first["dbname"] == "control_tower"
    assert first["user"] == "ct"
    assert first["sslmode"] == "require"
    assert (first["password"], second["password"]) == ("before-rotation", "after-rotation")
