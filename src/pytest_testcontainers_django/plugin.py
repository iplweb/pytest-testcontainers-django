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
from dataclasses import replace
from pathlib import Path

import pytest

from pytest_testcontainers_django import config as _config
from pytest_testcontainers_django._types import DjangoContainerConfig
from pytest_testcontainers_django.containers import (
    ContainerHandle,
    DockerNotRunningError,
    reuse_name,
    ryuk_maybe_running,
    shutdown_ryuk,
    start_postgres,
    start_redis,
)
from pytest_testcontainers_django.errors import BaselineMissingError, ReuseStaleContainerError
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
    """SPEC §10.3 Path B — auto-prepend django-pg-baseline's path when flagged.

    Returns a new config; never mutates the caller's lists, so re-invocation
    in the same process (pytester scenarios) doesn't keep prepending the
    baseline path.
    """
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
    new_pg = replace(
        config.postgres,
        init_scripts=[baseline, *config.postgres.init_scripts],
    )
    return replace(config, postgres=new_pg)


# Django's two "not ready yet" signals, both of which a rootdir-conftest
# preload can legitimately trigger before pytest-django has run
# ``django.setup()``.  Matched by class name (not isinstance) so the plugin
# never imports Django itself — preload runs before Django is configured and
# the plugin must work where Django is loaded lazily.  ``apps.is_installed()``
# (called at import by ``model_bakery`` >= 1.20) raises ``ImproperlyConfigured``
# when ``DJANGO_SETTINGS_MODULE`` is unset, but ``AppRegistryNotReady`` when
# settings *are* configured yet the app registry has not been populated — the
# latter is a sibling class, NOT a subclass, so it must be listed explicitly.
_DJANGO_NOT_READY_EXC_NAMES = frozenset({"ImproperlyConfigured", "AppRegistryNotReady"})


def _is_django_not_ready(exc: BaseException) -> bool:
    """Walk ``__cause__`` / ``__context__`` for a Django "not ready yet" signal.

    Returns ``True`` if any exception in the chain is one of
    :data:`_DJANGO_NOT_READY_EXC_NAMES` (matched by type name) — meaning the
    preload import failed only because Django settings/apps were not ready,
    which is benign: pytest's normal trylast loader re-imports the conftest
    later, after ``django.setup()``, where it succeeds.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if type(cur).__name__ in _DJANGO_NOT_READY_EXC_NAMES:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _reset_django_settings_if_loaded() -> None:
    """Evict Django's cached settings if a preload triggered an early load.

    The rootdir conftest can transitively import code that touches
    ``django.conf.settings`` at module top level (e.g. a stray
    ``from django.utils.translation import activate``).  When that happens
    during :func:`_preload_rootdir_conftest`, Django's lazy ``settings``
    binds to a ``Settings`` instance built from the **pre-injection**
    environment — ``DATABASES["default"]["PORT"]`` is frozen to whatever
    was in ``os.environ`` *before* we started a container and updated it.

    Pytest-django's later ``django.setup()`` reuses that cached Settings
    (and the already-imported settings module — Python caches modules in
    ``sys.modules``), so test sessions open psycopg connections to the
    stale port.  Symptom: ``connection to server at "localhost", port 5432
    failed`` while ``DJANGO_BPP_DB_PORT`` in ``os.environ`` is the random
    container port.

    The pytest-xdist controller doesn't observe this because workers are
    fresh subprocesses that re-import everything against the injected env;
    only the **serial** path (no ``-n``) hits the cached settings, which
    is why "with ``-n auto`` it works, serially it fails" is the classic
    bug report.

    Fix: drop ``LazySettings._wrapped`` *and* evict the user's settings
    package from ``sys.modules`` so the next access re-runs module-level
    code (where ``DATABASES`` is built from ``env("…_DB_PORT")``) against
    the now-corrected environment.

    No-op if Django was never imported, settings aren't configured, or
    ``SETTINGS_MODULE`` is missing (``settings.configure()`` was used
    in-process).
    """
    if "django.conf" not in sys.modules:
        return
    try:
        from django.conf import empty, settings
    except Exception:  # pragma: no cover — defensive
        logger.exception("could not import django.conf to reset settings")
        return
    if not settings.configured:
        return
    settings_module = getattr(settings, "SETTINGS_MODULE", None)
    settings._wrapped = empty
    if not settings_module:
        return
    # Settings files often import siblings (e.g. ``local.py`` does
    # ``from .base import *``).  Evict the whole settings sub-package so
    # module-level code (the ``DATABASES = {...}`` dict, etc.) re-runs
    # against the corrected environment.
    pkg_prefix = settings_module.rsplit(".", 1)[0] + "." if "." in settings_module else None
    for name in list(sys.modules):
        if name == settings_module or (pkg_prefix is not None and name.startswith(pkg_prefix)):
            del sys.modules[name]


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
    # pytest changed its private conftest-loading API across majors, so we
    # dispatch on method presence (feature detection) rather than a version
    # number:
    #   * pytest >= 8.0 exposes ``_loadconftestmodules(path, importmode, rootpath,
    #     *, consider_namespace_packages)`` together with the matching ini option.
    #   * pytest 7.x has no such method; ``_getconftestmodules(path, importmode,
    #     rootpath)`` performs the same conftest-import side effect and has no
    #     namespace-packages concept (the ini does not exist there either).
    loadconftestmodules = getattr(pluginmanager, "_loadconftestmodules", None)
    try:
        if loadconftestmodules is not None:
            loadconftestmodules(
                rootpath,
                importmode,
                rootpath,
                consider_namespace_packages=bool(
                    early_config.getini("consider_namespace_packages")
                ),
            )
        else:
            pluginmanager._getconftestmodules(  # type: ignore[attr-defined]
                rootpath,
                importmode,
                rootpath,
            )
    except Exception as exc:
        if _is_django_not_ready(exc):
            # Expected at preload time: the conftest transitively imports code
            # that touches Django settings/apps (e.g. model_bakery>=1.20 calls
            # apps.is_installed() at module import). Our hook runs before
            # pytest-django has run django.setup(), so the import fails here
            # with either ImproperlyConfigured (settings module unset) or
            # AppRegistryNotReady (settings configured, app registry not yet
            # populated).  Pytest's own trylast loader will re-import the
            # conftest later (after Django is set up *and* after our env
            # injection has populated DB host/port), where it will succeed.
            # Caveat: any register() calls *before* the failing import won't
            # run early — users hitting this should configure via
            # pyproject.toml's [tool.pytest-testcontainers-django] instead,
            # or move the offending import.
            logger.debug(
                "preloading rootdir conftest.py deferred: Django not ready "
                "yet (will be re-imported by pytest's normal loader)"
            )
            return
        # Real conftest error — pytest's trylast loader would re-raise it
        # with a full traceback anyway; we surface it here too so users see
        # it once even if something masks the later re-raise.
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

    if _config.is_disabled(config, args):
        return

    if _is_xdist_worker():
        # Worker branch (SPEC §7): containers were already started by the
        # controller and env was inherited at fork; we only need to keep
        # dotenv from clobbering it on settings re-import.
        _env_snapshot = inject_worker(config)
        return

    # Order matters: baseline resolution may add init scripts; the template
    # default (SPEC §10.6) reads init_scripts to decide whether to default
    # ``postgres_template = postgres_database``.  Resolving baseline first
    # ensures use_django_pg_baseline=true triggers the template default
    # even when the user didn't list any other postgres_init_scripts.
    config = _resolve_baseline_path(config)
    config = _config.apply_template_default(config)
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
    except ReuseStaleContainerError as exc:
        raise pytest.UsageError(f"[pytest-testcontainers-django] {exc}") from None

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

    # If the rootdir-conftest preload triggered an early Django settings
    # load (e.g. via a top-level ``from django.utils.translation import
    # activate``), the cached Settings instance was built before our env
    # injection — drop it so pytest-django's ``django.setup()`` re-reads
    # the now-correct ports.  Critical for the serial (no-``-n``) path;
    # xdist workers don't observe this because they're fresh subprocesses.
    _reset_django_settings_if_loaded()

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

    # Ryuk last: it is the net for anything the explicit stops missed, and
    # testcontainers never shuts it down on its own — it would sit there
    # holding a privileged rw mount of the Docker socket until its
    # reconnection timeout expires. Guarded so a run that started nothing
    # (plugin disabled, xdist worker) doesn't import testcontainers for it.
    if ryuk_maybe_running():
        shutdown_ryuk()

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
