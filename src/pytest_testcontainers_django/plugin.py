"""The pytest plugin module.

Owns the ``pytest_load_initial_conftests(tryfirst=True)`` hook — the
core IP of this package (SPEC §1, §6).  Auto-loaded via the ``pytest11``
entry point in pyproject.toml.

Sequence (SPEC §6.5):

1. Plugin imported (top-level code runs; user's conftest ``register()``
   has already executed).
2. ``pytest_load_initial_conftests(tryfirst=True)`` fires before
   pytest-django's hook of the same name.
3. We resolve config, start containers via :mod:`.containers`, write
   ``os.environ``, register an atexit safety net.
4. pytest-django's hook runs next: imports settings, reads our env vars.

xdist workers (SPEC §7) skip container start — they inherit the
controller's env vars on fork — but still set the skip-dotenv flag.
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
from pathlib import Path

import pytest

from pytest_testcontainers_django import config as _config
from pytest_testcontainers_django._types import DjangoContainerConfig
from pytest_testcontainers_django.containers import (
    ContainerHandle,
    DockerNotRunningError,
    reuse_name,
    start_postgres,
    start_redis,
)
from pytest_testcontainers_django.errors import BaselineMissingError
from pytest_testcontainers_django.injection import (
    _Snapshot,
    inject,
    inject_worker,
    restore,
)

logger = logging.getLogger("pytest_testcontainers_django")

# Module-level state. Set in pytest_load_initial_conftests; consumed
# by pytest_unconfigure and the atexit safety net.
_pg_handle: ContainerHandle | None = None
_redis_handle: ContainerHandle | None = None
_env_snapshot: _Snapshot | None = None
_reuse_active: bool = False
_atexit_registered: bool = False


# NOTE: ``--no-testcontainers`` is registered by #1 (pytest-testcontainers).
# We do not redefine it here — argparse would raise on the duplicate.
# We still observe its presence in ``args`` via :func:`config.is_disabled`
# so this plugin's own env injection respects the same flag.


def _is_xdist_worker() -> bool:
    """Return True inside a pytest-xdist worker process. SPEC §7."""
    return "PYTEST_XDIST_WORKER" in os.environ


def _resolve_baseline_path(config: DjangoContainerConfig) -> DjangoContainerConfig:
    """SPEC §10.3 Path B — auto-prepend django-pg-baseline's path when flagged."""
    if not config.use_django_pg_baseline:
        return config
    try:
        from django_pg_baseline import get_baseline_path
    except ImportError as exc:
        raise BaselineMissingError(
            "use_django_pg_baseline=true but `django-pg-baseline` is not "
            "installed. Install it (e.g. `pip install pytest-testcontainers-django"
            "[baseline]`) or set the flag to false."
        ) from exc
    baseline = Path(get_baseline_path())
    config.postgres.init_scripts.insert(0, baseline)
    return config


def _preload_rootdir_conftest(early_config: pytest.Config) -> None:
    """Force-import the rootdir ``conftest.py`` so its ``register()`` calls run.

    Pytest's own conftest loader is registered with ``trylast=True``
    (``_pytest/config/__init__.py``), so by default it runs *after* our
    ``tryfirst`` hook — which means ``register()`` calls would not execute
    in time.  We call pytest's private ``_loadconftestmodules`` ourselves
    against the rootdir so the user's ``conftest.py`` is imported and
    its top-level code (``register(...)``) runs before we read config.
    The later trylast call by pytest core is idempotent — pytest caches
    by path so the conftest is only really loaded once.
    """
    pluginmanager = early_config.pluginmanager
    rootpath = Path(getattr(early_config, "rootpath", Path.cwd()))
    importmode = early_config.getoption("importmode", default="prepend")
    consider_namespace_packages = bool(early_config.getini("consider_namespace_packages"))
    try:
        pluginmanager._loadconftestmodules(  # type: ignore[attr-defined]
            rootpath,
            importmode,
            rootpath,
            consider_namespace_packages=consider_namespace_packages,
        )
    except Exception:
        # Conftest import failure here would re-raise from pytest's own
        # trylast loader anyway — we just preload, never swallow.
        logger.exception("preloading rootdir conftest.py raised; continuing")


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(
    early_config: pytest.Config,
    parser: pytest.Parser,
    args: list[str],
) -> None:
    """SPEC §6: start containers and inject env BEFORE pytest-django's hook.

    The ``@pytest.hookimpl(tryfirst=True)`` decorator is load-bearing;
    do not remove it. See SPEC §6.4.
    """
    global _pg_handle, _redis_handle, _env_snapshot, _reuse_active, _atexit_registered

    _preload_rootdir_conftest(early_config)

    rootdir = Path(getattr(early_config, "rootpath", Path.cwd()))
    config = _config.load_config(rootdir)
    config = _config.apply_template_default(config)

    if _config.is_disabled(config, args):
        return

    if _is_xdist_worker():
        # Worker branch (SPEC §7): containers were already started by the
        # controller and env was inherited at fork; we only need to keep
        # dotenv from clobbering it on settings re-import.
        _env_snapshot = inject_worker(config)
        return

    config = _resolve_baseline_path(config)
    _config.validate(config)

    _reuse_active = _config.is_reuse_enabled(config)
    pg_reuse_name = reuse_name("psql") if _reuse_active else None
    redis_reuse_name = reuse_name("redis") if _reuse_active else None

    try:
        _pg_handle = start_postgres(
            image=config.postgres.image,
            user=config.postgres.user,
            password=config.postgres.password,
            database=config.postgres.database,
            internal_port=config.postgres.internal_port,
            env=config.postgres.env,
            init_scripts=config.postgres.init_scripts,
            reuse_name=pg_reuse_name,
        )
        if config.redis is not None:
            _redis_handle = start_redis(
                image=config.redis.image,
                internal_port=config.redis.internal_port,
                reuse_name=redis_reuse_name,
            )
    except DockerNotRunningError as exc:
        raise pytest.UsageError(
            "[pytest-testcontainers-django] Docker daemon is not reachable. "
            "Is Docker running?\n"
            "  - Start Docker and re-run pytest, OR\n"
            f"  - Disable testcontainers: pass --no-testcontainers or set "
            f"{config.disable_env}=1 (and ensure your DB is reachable on "
            "the host:port your settings expect).\n"
            f"Underlying error: {exc}"
        ) from None

    if (
        _reuse_active
        and config.postgres.init_scripts
        and _pg_handle is not None
        and _pg_handle.is_bound_to_existing
    ):
        # SPEC §10.7
        print(
            "[pytest-testcontainers-django] reuse mode + init_scripts: "
            "init scripts NOT replayed against the existing container "
            "(Postgres only runs /docker-entrypoint-initdb.d/ on first init).\n"
            "To re-apply: stop and remove the container, then re-run.\n"
            f"Suggested: docker rm -f {_pg_handle.name}",
            file=sys.stderr,
        )

    _env_snapshot = inject(
        config,
        db_host=_pg_handle.host,
        db_port=_pg_handle.port,
        redis_host=_redis_handle.host if _redis_handle is not None else None,
        redis_port=_redis_handle.port if _redis_handle is not None else None,
    )

    if not _atexit_registered:
        atexit.register(_atexit_stop)
        _atexit_registered = True


def pytest_unconfigure(config: pytest.Config) -> None:
    """Stop containers (unless reuse mode) and restore env."""
    _stop_and_restore(reuse=_reuse_active)


def _atexit_stop() -> None:
    """Safety net for abrupt-exit paths that skip ``pytest_unconfigure``."""
    _stop_and_restore(reuse=_reuse_active)


def _stop_and_restore(*, reuse: bool) -> None:
    global _pg_handle, _redis_handle, _env_snapshot

    if not reuse:
        if _pg_handle is not None:
            try:
                _pg_handle.stop()
            except Exception:
                logger.exception("error stopping postgres container")
        if _redis_handle is not None:
            try:
                _redis_handle.stop()
            except Exception:
                logger.exception("error stopping redis container")
    _pg_handle = None
    _redis_handle = None

    if _env_snapshot is not None:
        try:
            restore(_env_snapshot)
        finally:
            _env_snapshot = None


def _reset_state() -> None:
    """Test-only helper: wipe module-level state between scenarios."""
    global _pg_handle, _redis_handle, _env_snapshot, _reuse_active, _atexit_registered
    _pg_handle = None
    _redis_handle = None
    _env_snapshot = None
    _reuse_active = False
    _atexit_registered = False


__all__ = [
    "pytest_load_initial_conftests",
    "pytest_unconfigure",
]
