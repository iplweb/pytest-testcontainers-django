"""Honor the active ``docker context`` when resolving the daemon socket.

``docker.from_env()`` — used by docker-py, by testcontainers' own client, and
by Ryuk's socket bind-mount — honors ``DOCKER_HOST`` but, unlike the ``docker``
CLI, ignores the active ``docker context``. On OrbStack / colima / a stopped
Docker Desktop the daemon lives on a non-default socket and
``/var/run/docker.sock`` is absent or a dangling symlink, so the plugin
reported ``[pytest-testcontainers-django] Docker daemon is not reachable`` even
though ``docker ps`` worked fine.

We export ``DOCKER_HOST`` from the active context before any client is built.
An explicit ``DOCKER_HOST`` always wins; no resolvable context falls back to
the platform default socket. These tests pin that precedence.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from pytest_testcontainers_django import containers


@pytest.fixture(autouse=True)
def _restore_docker_host() -> None:
    """``_ensure_docker_host_env`` mutates os.environ directly; restore it."""
    original = os.environ.get("DOCKER_HOST")
    yield
    if original is None:
        os.environ.pop("DOCKER_HOST", None)
    else:
        os.environ["DOCKER_HOST"] = original


def test_ensure_docker_host_env_exports_active_context_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    class FakeCtx:
        Host = "unix:///Users/me/.orbstack/run/docker.sock"

    monkeypatch.setattr(containers, "_active_docker_context", lambda: FakeCtx())

    containers._ensure_docker_host_env()

    assert os.environ["DOCKER_HOST"] == "unix:///Users/me/.orbstack/run/docker.sock"


def test_ensure_docker_host_env_respects_existing_docker_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/explicit.sock")

    def fail() -> None:
        raise AssertionError("must not resolve context when DOCKER_HOST is set")

    monkeypatch.setattr(containers, "_active_docker_context", fail)

    containers._ensure_docker_host_env()

    assert os.environ["DOCKER_HOST"] == "unix:///tmp/explicit.sock"


def test_ensure_docker_host_env_noop_without_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(containers, "_active_docker_context", lambda: None)

    containers._ensure_docker_host_env()

    assert "DOCKER_HOST" not in os.environ


def test_active_docker_context_returns_current_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from docker.context import ContextAPI

    sentinel = object()
    monkeypatch.setattr(ContextAPI, "get_current_context", classmethod(lambda cls: sentinel))

    assert containers._active_docker_context() is sentinel


def test_active_docker_context_swallows_errors_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from docker.context import ContextAPI

    def boom(cls) -> None:
        raise RuntimeError("context resolution exploded")

    monkeypatch.setattr(ContextAPI, "get_current_context", classmethod(boom))

    assert containers._active_docker_context() is None


def test_check_daemon_exports_docker_host_from_context_before_ping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_check_daemon must export DOCKER_HOST from the active context so the ping
    — and every later testcontainers/Ryuk client — resolves the socket the CLI
    uses, instead of the absent/dangling platform default."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    class FakeCtx:
        Host = "unix:///Users/me/.orbstack/run/docker.sock"

    monkeypatch.setattr(containers, "_active_docker_context", lambda: FakeCtx())

    class _OK:
        def ping(self) -> None:
            pass

    class _DockerException(Exception):
        pass

    fake_errors = type("errors", (), {"DockerException": _DockerException})
    fake_docker = type(
        "docker", (), {"from_env": staticmethod(lambda: _OK()), "errors": fake_errors}
    )
    with patch.dict("sys.modules", {"docker": fake_docker, "docker.errors": fake_errors}):
        containers._check_daemon()

    assert os.environ["DOCKER_HOST"] == "unix:///Users/me/.orbstack/run/docker.sock"
