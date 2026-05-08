"""Env-var injection tests (SPEC §6.5 step 9, §9)."""

from __future__ import annotations

import os

import pytest

from pytest_testcontainers_django import (
    DjangoContainerConfig,
    PostgresService,
    RedisService,
)
from pytest_testcontainers_django.injection import inject, inject_worker, restore


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch):
    keys = [
        "DJANGO_DB_HOST",
        "DJANGO_DB_PORT",
        "DJANGO_DB_NAME",
        "DJANGO_DB_USER",
        "DJANGO_DB_PASSWORD",
        "DJANGO_DB_TEST_TEMPLATE",
        "DJANGO_REDIS_HOST",
        "DJANGO_REDIS_PORT",
        "DJANGO_SKIP_DOTENV",
        "MYAPP_DB_HOST",
        "MYAPP_DB_PORT",
        "MYAPP_SKIP_DOTENV",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    yield


def test_inject_writes_default_db_env_vars() -> None:
    cfg = DjangoContainerConfig()
    snap = inject(cfg, db_host="localhost", db_port=63403, redis_host=None, redis_port=None)
    assert os.environ["DJANGO_DB_HOST"] == "localhost"
    assert os.environ["DJANGO_DB_PORT"] == "63403"
    assert os.environ["DJANGO_DB_NAME"] == "postgres"
    assert os.environ["DJANGO_DB_USER"] == "postgres"
    assert os.environ["DJANGO_DB_PASSWORD"] == "postgres"
    assert os.environ["DJANGO_SKIP_DOTENV"] == "1"
    assert "DJANGO_DB_TEST_TEMPLATE" not in os.environ
    restore(snap)
    assert "DJANGO_DB_HOST" not in os.environ


def test_inject_writes_template_env_when_set() -> None:
    cfg = DjangoContainerConfig(
        postgres=PostgresService(database="myapp", template="myapp"),
    )
    snap = inject(cfg, db_host="127.0.0.1", db_port=1234, redis_host=None, redis_port=None)
    assert os.environ["DJANGO_DB_TEST_TEMPLATE"] == "myapp"
    restore(snap)


def test_inject_skips_skip_dotenv_when_disabled() -> None:
    cfg = DjangoContainerConfig(skip_dotenv_env="")
    snap = inject(cfg, db_host="h", db_port=1, redis_host=None, redis_port=None)
    assert "DJANGO_SKIP_DOTENV" not in os.environ
    restore(snap)


def test_inject_writes_redis_env_when_configured() -> None:
    cfg = DjangoContainerConfig(redis=RedisService())
    snap = inject(cfg, db_host="h", db_port=1, redis_host="h2", redis_port=2222)
    assert os.environ["DJANGO_REDIS_HOST"] == "h2"
    assert os.environ["DJANGO_REDIS_PORT"] == "2222"
    restore(snap)


def test_inject_uses_custom_env_names() -> None:
    cfg = DjangoContainerConfig(
        postgres=PostgresService(
            env_names={
                "host": "MYAPP_DB_HOST",
                "port": "MYAPP_DB_PORT",
                "name": "MYAPP_DB_NAME",
                "user": "MYAPP_DB_USER",
                "password": "MYAPP_DB_PASSWORD",
                "test_template": "MYAPP_DB_TEST_TEMPLATE",
            },
        ),
        skip_dotenv_env="MYAPP_SKIP_DOTENV",
    )
    snap = inject(cfg, db_host="h", db_port=11, redis_host=None, redis_port=None)
    assert os.environ["MYAPP_DB_HOST"] == "h"
    assert os.environ["MYAPP_DB_PORT"] == "11"
    assert os.environ["MYAPP_SKIP_DOTENV"] == "1"
    restore(snap)


def test_inject_worker_only_sets_skip_dotenv() -> None:
    cfg = DjangoContainerConfig()
    snap = inject_worker(cfg)
    assert os.environ["DJANGO_SKIP_DOTENV"] == "1"
    assert "DJANGO_DB_HOST" not in os.environ
    restore(snap)
    assert "DJANGO_SKIP_DOTENV" not in os.environ


def test_restore_replaces_pre_existing_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DJANGO_DB_PORT", "5432")
    cfg = DjangoContainerConfig()
    snap = inject(cfg, db_host="h", db_port=63403, redis_host=None, redis_port=None)
    assert os.environ["DJANGO_DB_PORT"] == "63403"
    restore(snap)
    assert os.environ["DJANGO_DB_PORT"] == "5432"
