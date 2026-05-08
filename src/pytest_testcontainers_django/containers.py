"""Container start/stop bridge.

This is the only module that talks to ``testcontainers-python`` directly.
The Django plugin code calls :func:`start_postgres` / :func:`start_redis`
and gets back a small handle exposing ``host``, ``port``, ``stop()``.

Why not simply call ``pytest_testcontainers.make_postgres``? Two
practical reasons specific to the Django timing dance:

* We need ``with_volume_mapping`` calls **before** ``.start()`` to mount
  init scripts into ``/docker-entrypoint-initdb.d/`` (SPEC §10).  #1's
  ``make_postgres`` doesn't expose pre-start mutation.
* We want to detect "we attached to a pre-existing container" so we can
  emit the SPEC §10.7 reuse warning.

We *do* lean on #1 for: daemon ping (:class:`pytest_testcontainers.DockerNotRunningError`),
atexit registration helpers, Ryuk-disable choreography, and
named-container reuse lookup.  Those are imported from #1's surface so
breakage is contained at one spot if #1 evolves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pytest_testcontainers import DockerNotRunningError
from pytest_testcontainers.containers import (
    bind_to_running,
    deregister_atexit_stop,
    disable_ryuk_once,
    find_existing_container,
    register_atexit_stop,
    restart_stopped_or_raise,
    start_or_raise,
)
from pytest_testcontainers.reuse import reuse_name_for

from pytest_testcontainers_django.errors import ReuseStaleContainerError

logger = logging.getLogger("pytest_testcontainers_django")


@dataclass
class ContainerHandle:
    """Minimal handle returned by :func:`start_postgres` / :func:`start_redis`."""

    host: str
    port: int
    name: str | None
    is_bound_to_existing: bool
    _instance: Any  # testcontainers DockerContainer; we only call .stop() on it
    _owns_instance: bool  # False when bound to a pre-existing reuse container

    def stop(self) -> None:
        if not self._owns_instance:
            return
        deregister_atexit_stop(self._instance)
        try:
            self._instance.stop()
        except Exception:
            logger.exception("cleanup: container.stop() failed")


def _check_daemon() -> None:
    """Wrap #1's daemon ping so callers get a stable exception class."""
    import docker
    import docker.errors

    try:
        docker.from_env().ping()
    except docker.errors.DockerException as exc:
        raise DockerNotRunningError(str(exc)) from exc


def _start_or_attach(
    container: Any,
    *,
    reuse_name: str | None,
) -> tuple[Any, bool]:
    """Start *container*, or attach to a pre-existing one when reuse is on.

    Returns ``(instance, is_bound_to_existing)``.  When bound to existing,
    the caller MUST NOT register atexit stop and MUST NOT call ``stop()``.
    """
    if reuse_name is None:
        return container, False

    disable_ryuk_once()
    existing = find_existing_container(reuse_name)
    if existing is None:
        container.with_name(reuse_name)
        return container, False

    container_cls = type(container)
    status = getattr(existing, "status", "")
    if status == "running":
        return bind_to_running(container_cls, existing), True
    if status in {"exited", "created", "paused"}:
        restart_stopped_or_raise(existing, reuse_name)
        return bind_to_running(container_cls, existing), True
    # "dead" / "removing" / unknown: a fresh start would fail with a 409
    # name conflict against the stale container, so surface an actionable
    # error.  The user has to remove the stale container manually because
    # we won't take destructive action on shared docker state.
    raise ReuseStaleContainerError(
        f"reuse-mode container {reuse_name!r} exists but is in state {status!r} — "
        f"it cannot be restarted and starting a fresh container would conflict on "
        f"the name. Remove it manually:\n"
        f"  docker rm -f {reuse_name}"
    )


def start_postgres(
    *,
    image: str,
    user: str,
    password: str,
    database: str,
    internal_port: int,
    env: dict[str, str],
    init_scripts: list[Path],
    reuse_name: str | None,
) -> ContainerHandle:
    """Construct + start (or attach to) a PostgresContainer."""
    from testcontainers.postgres import PostgresContainer

    _check_daemon()

    container = PostgresContainer(
        image=image,
        username=user,
        password=password,
        dbname=database,
        port=internal_port,
        driver=None,
    )
    for key, value in env.items():
        container.with_env(key, value)
    for index, script in enumerate(init_scripts):
        # SPEC §10.2: NN-name.sql, ordered.
        container.with_volume_mapping(
            str(script),
            f"/docker-entrypoint-initdb.d/{index:02d}-{script.name}",
            "ro",
        )

    instance, bound = _start_or_attach(container, reuse_name=reuse_name)
    if bound:
        host, port = _read_existing_host_port(instance, internal_port)
        return ContainerHandle(
            host=host,
            port=port,
            name=reuse_name,
            is_bound_to_existing=True,
            _instance=instance,
            _owns_instance=False,
        )

    start_or_raise(instance, image)
    if reuse_name is None:
        register_atexit_stop(instance)

    host = instance.get_container_host_ip()
    port = int(instance.get_exposed_port(internal_port))
    return ContainerHandle(
        host=_normalize_host(host),
        port=port,
        name=reuse_name,
        is_bound_to_existing=False,
        _instance=instance,
        _owns_instance=reuse_name is None,
    )


def start_redis(
    *,
    image: str,
    internal_port: int,
    reuse_name: str | None,
) -> ContainerHandle:
    """Construct + start (or attach to) a RedisContainer."""
    from testcontainers.redis import RedisContainer

    _check_daemon()

    container = RedisContainer(image=image, port=internal_port)
    instance, bound = _start_or_attach(container, reuse_name=reuse_name)
    if bound:
        host, port = _read_existing_host_port(instance, internal_port)
        return ContainerHandle(
            host=host,
            port=port,
            name=reuse_name,
            is_bound_to_existing=True,
            _instance=instance,
            _owns_instance=False,
        )

    start_or_raise(instance, image)
    if reuse_name is None:
        register_atexit_stop(instance)

    host = instance.get_container_host_ip()
    port = int(instance.get_exposed_port(internal_port))
    return ContainerHandle(
        host=_normalize_host(host),
        port=port,
        name=reuse_name,
        is_bound_to_existing=False,
        _instance=instance,
        _owns_instance=reuse_name is None,
    )


def _normalize_host(host: str) -> str:
    if host in ("", "0.0.0.0"):
        return "localhost"
    return host


def _read_existing_host_port(instance: Any, internal_port: int) -> tuple[str, int]:
    """Read host/port from a docker-py container the testcontainers wrapper holds.

    ``bind_to_running`` sets ``instance._container`` to the docker-py
    Container object; that's where the port mapping lives.
    """
    docker_container = getattr(instance, "_container", None)
    if docker_container is None:  # pragma: no cover — defensive
        host = instance.get_container_host_ip()
        port = int(instance.get_exposed_port(internal_port))
        return _normalize_host(host), port
    ports = docker_container.attrs["NetworkSettings"]["Ports"]
    binding = (ports.get(f"{internal_port}/tcp") or [{}])[0]
    host = binding.get("HostIp", "localhost")
    return _normalize_host(host), int(binding["HostPort"])


def reuse_name(service_slug: str) -> str:
    """Compose a reuse-name for the eager-start path. Single-shared per controller."""
    return reuse_name_for(service_slug)


__all__ = [
    "ContainerHandle",
    "DockerNotRunningError",
    "reuse_name",
    "start_postgres",
    "start_redis",
]
