"""Ephemeral containers must be named, and Ryuk must be shut down.

Before this, the non-reuse path never called ``with_name()``, so Docker
assigned names like ``goofy_torvalds``. Two consequences, both of which
bite on a developer machine: ``pytest --testcontainers-clean`` matches on
the ``<project>-tc-`` prefix and could never reach them, and a human
staring at ``docker ps`` had no way to tell which run a stray Postgres
belonged to.
"""

from __future__ import annotations

from typing import Any

import pytest

from pytest_testcontainers_django import containers


class _FakeContainer:
    def __init__(self) -> None:
        self.name: str | None = None

    def with_name(self, name: str) -> _FakeContainer:
        self.name = name
        return self


# --------------------------------------------------------------------------
# Name composition
# --------------------------------------------------------------------------
def test_ephemeral_name_carries_project_and_service(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTEST_TESTCONTAINERS_PROJECT", "myproj")
    name = containers.ephemeral_name("psql")
    assert name.startswith("myproj-tc-psql-")


def test_ephemeral_names_are_unique_per_call(tmp_path, monkeypatch) -> None:
    """Two concurrent runs on one branch must not collide on the name."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTEST_TESTCONTAINERS_PROJECT", "myproj")
    assert containers.ephemeral_name("psql") != containers.ephemeral_name("psql")


def test_ephemeral_name_matches_the_clean_command_prefix(tmp_path, monkeypatch) -> None:
    """The whole point: ``--testcontainers-clean`` can now reach these."""
    from pytest_testcontainers import reuse

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTEST_TESTCONTAINERS_PROJECT", "myproj")
    prefix = f"{reuse.sanitize_component(reuse.project_name())}-tc-"
    assert containers.ephemeral_name("redis").startswith(prefix)


def test_reuse_name_is_stable_across_calls(tmp_path, monkeypatch) -> None:
    """Reuse names must stay byte-stable — find-or-create depends on it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTEST_TESTCONTAINERS_PROJECT", "myproj")
    assert containers.reuse_name("psql") == containers.reuse_name("psql")


# --------------------------------------------------------------------------
# _start_or_attach naming
# --------------------------------------------------------------------------
def test_non_reuse_container_gets_an_explicit_name() -> None:
    container = _FakeContainer()
    instance, bound = containers._start_or_attach(
        container, reuse_name=None, fallback_name="myproj-tc-psql-main-ab12-master-9f3c"
    )
    assert instance is container
    assert bound is False
    assert container.name == "myproj-tc-psql-main-ab12-master-9f3c"


def test_reuse_container_uses_the_reuse_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(containers, "disable_ryuk_once", lambda: None)
    monkeypatch.setattr(containers, "find_existing_container", lambda name: None)

    container = _FakeContainer()
    instance, bound = containers._start_or_attach(
        container, reuse_name="myproj-tc-psql-main-ab12-master", fallback_name="ignored"
    )
    assert instance is container
    assert bound is False
    assert container.name == "myproj-tc-psql-main-ab12-master"


def test_bound_existing_container_is_not_renamed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attaching to a live container must not touch it."""

    class _Existing:
        status = "running"

    monkeypatch.setattr(containers, "disable_ryuk_once", lambda: None)
    monkeypatch.setattr(containers, "find_existing_container", lambda name: _Existing())
    monkeypatch.setattr(containers, "bind_to_running", lambda cls, existing: "bound")

    container = _FakeContainer()
    instance, bound = containers._start_or_attach(
        container, reuse_name="myproj-tc-psql-main-ab12-master", fallback_name="ignored"
    )
    assert instance == "bound"
    assert bound is True
    assert container.name is None


# --------------------------------------------------------------------------
# Ryuk shutdown
# --------------------------------------------------------------------------
def test_stop_and_restore_shuts_ryuk_down(monkeypatch: pytest.MonkeyPatch) -> None:
    from pytest_testcontainers_django import plugin

    calls: list[str] = []
    monkeypatch.setattr(plugin, "ryuk_maybe_running", lambda: True)
    monkeypatch.setattr(plugin, "shutdown_ryuk", lambda: calls.append("ryuk"))
    plugin._stop_and_restore(reuse=False)
    assert calls == ["ryuk"]


def test_stop_and_restore_shuts_ryuk_down_after_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pytest_testcontainers_django import plugin

    order: list[str] = []

    class _Handle:
        def stop(self) -> None:
            order.append("container")

    monkeypatch.setattr(plugin, "_pg_handle", _Handle())
    monkeypatch.setattr(plugin, "ryuk_maybe_running", lambda: True)
    monkeypatch.setattr(plugin, "shutdown_ryuk", lambda: order.append("ryuk"))
    plugin._stop_and_restore(reuse=False)
    assert order == ["container", "ryuk"]


def test_stop_and_restore_skips_ryuk_when_nothing_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled run or an xdist worker must not import testcontainers."""
    from pytest_testcontainers_django import plugin

    monkeypatch.setattr(plugin, "ryuk_maybe_running", lambda: False)
    monkeypatch.setattr(
        plugin, "shutdown_ryuk", lambda: pytest.fail("reaper touched with nothing running")
    )
    plugin._stop_and_restore(reuse=False)


def test_reuse_mode_still_shuts_ryuk_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Harmless (reuse never starts one) and keeps the path uniform."""
    from pytest_testcontainers_django import plugin

    calls: list[Any] = []
    monkeypatch.setattr(plugin, "ryuk_maybe_running", lambda: True)
    monkeypatch.setattr(plugin, "shutdown_ryuk", lambda: calls.append("ryuk"))
    plugin._stop_and_restore(reuse=True)
    assert calls == ["ryuk"]
