"""``os.environ`` writer (SPEC §6.5 step 9).

Kept separate from the plugin module so unit tests can poke it directly
without dragging in pytest-django.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

from pytest_testcontainers_django._types import (
    DjangoContainerConfig,
    PostgresService,
    RedisService,
)


@dataclass
class _Snapshot:
    """Records what we wrote so :func:`restore` can put it back exactly."""

    keys: tuple[str, ...]
    previous: dict[str, str | None]


def inject(
    config: DjangoContainerConfig,
    *,
    db_host: str,
    db_port: int,
    redis_host: str | None,
    redis_port: int | None,
) -> _Snapshot:
    """Write the eager-start values into ``os.environ``.

    Returns a snapshot that records the previous values, so cleanup can
    restore the environment to its pre-injection state.
    """
    pg = config.postgres
    written: dict[str, str] = {}

    written[pg.env_names["host"]] = db_host
    written[pg.env_names["port"]] = str(db_port)
    written[pg.env_names["name"]] = pg.database
    written[pg.env_names["user"]] = pg.user
    written[pg.env_names["password"]] = pg.password
    if pg.template is not None and pg.env_names.get("test_template"):
        written[pg.env_names["test_template"]] = pg.template

    if config.redis is not None and redis_host is not None and redis_port is not None:
        redis_env = config.redis.env_names
        written[redis_env["host"]] = redis_host
        written[redis_env["port"]] = str(redis_port)

    if config.skip_dotenv_env:
        written[config.skip_dotenv_env] = "1"

    snapshot = _capture_keys(written.keys())
    for key, value in written.items():
        os.environ[key] = value
    return snapshot


def inject_worker(config: DjangoContainerConfig) -> _Snapshot:
    """Worker branch (SPEC §7): only force the skip-dotenv flag."""
    keys: list[str] = []
    if config.skip_dotenv_env:
        keys.append(config.skip_dotenv_env)
    snapshot = _capture_keys(keys)
    if config.skip_dotenv_env:
        os.environ[config.skip_dotenv_env] = "1"
    return snapshot


def restore(snapshot: _Snapshot) -> None:
    for key in snapshot.keys:
        prev = snapshot.previous.get(key)
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev


def _capture_keys(keys: Iterable[str]) -> _Snapshot:
    keys_tuple = tuple(keys)
    previous = {key: os.environ.get(key) for key in keys_tuple}
    return _Snapshot(keys=keys_tuple, previous=previous)


__all__ = [
    "PostgresService",
    "RedisService",
    "inject",
    "inject_worker",
    "restore",
]
