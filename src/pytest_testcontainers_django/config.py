"""Pyproject reader, ``register()`` API, validation.

Resolution order (SPEC §5.1):

1. ``register()`` value if ``conftest.py`` called it.
2. ``[tool.pytest-testcontainers-django]`` table in nearest pyproject.toml.
3. Built-in defaults from :mod:`pytest_testcontainers_django._types`.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover — exercised on 3.10
    import tomli as tomllib

from pytest_testcontainers_django._types import (
    DEFAULT_DB_ENV_NAMES,
    DEFAULT_REDIS_ENV_NAMES,
    DjangoContainerConfig,
    PostgresService,
    RedisService,
)
from pytest_testcontainers_django.errors import ConfigError

_REGISTERED: DjangoContainerConfig | None = None


def register(config: DjangoContainerConfig) -> None:
    """Register a programmatic configuration from ``conftest.py``.

    Overrides any ``[tool.pytest-testcontainers-django]`` pyproject table.
    Last call wins (mostly relevant in test scenarios).
    """
    global _REGISTERED
    if not isinstance(config, DjangoContainerConfig):
        raise TypeError(
            f"register() requires a DjangoContainerConfig instance, got {type(config).__name__}"
        )
    _REGISTERED = config


def _reset_registered() -> None:
    """Test-only helper. Not part of the public API."""
    global _REGISTERED
    _REGISTERED = None


def _find_pyproject(start: Path) -> Path | None:
    """Walk up from *start* looking for the nearest ``pyproject.toml``."""
    current = start.resolve()
    for candidate in [current, *current.parents]:
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file():
            return pyproject
    return None


def _load_pyproject_table(pyproject: Path) -> dict[str, Any]:
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    tool = data.get("tool", {})
    return tool.get("pytest-testcontainers-django", {}) or {}


def _resolve_init_scripts(raw: list[str | Path] | None, project_root: Path) -> list[Path]:
    if not raw:
        return []
    out: list[Path] = []
    for entry in raw:
        path = Path(entry)
        if not path.is_absolute():
            path = project_root / path
        out.append(path)
    return out


def _config_from_table(table: dict[str, Any], project_root: Path) -> DjangoContainerConfig:
    """Translate a parsed ``[tool.pytest-testcontainers-django]`` table.

    Unknown keys are ignored — TOML errors should be loud, but typos in
    nested optional keys shouldn't break the plugin's bootstrap. (We do
    raise on structurally impossible values, e.g. wrong types.)
    """
    db_env_names = dict(DEFAULT_DB_ENV_NAMES)
    db_env_names_overrides = {
        "host": table.get("db_host_env"),
        "port": table.get("db_port_env"),
        "name": table.get("db_name_env"),
        "user": table.get("db_user_env"),
        "password": table.get("db_password_env"),
        "test_template": table.get("db_test_template_env"),
    }
    for key, value in db_env_names_overrides.items():
        if value is not None:
            db_env_names[key] = str(value)

    postgres = PostgresService(
        image=str(table.get("postgres_image", PostgresService.image)),
        user=str(table.get("postgres_user", PostgresService.user)),
        password=str(table.get("postgres_password", PostgresService.password)),
        database=str(table.get("postgres_database", PostgresService.database)),
        internal_port=int(table.get("postgres_internal_port", PostgresService.internal_port)),
        template=(
            str(table["postgres_template"]) if table.get("postgres_template") is not None else None
        ),
        init_scripts=_resolve_init_scripts(table.get("postgres_init_scripts"), project_root),
        env=dict(table.get("postgres_env", {}) or {}),
        env_names=db_env_names,
    )

    redis: RedisService | None = None
    if table.get("redis_enabled", False):
        redis_env_names = dict(DEFAULT_REDIS_ENV_NAMES)
        if (override := table.get("redis_host_env")) is not None:
            redis_env_names["host"] = str(override)
        if (override := table.get("redis_port_env")) is not None:
            redis_env_names["port"] = str(override)
        redis = RedisService(
            image=str(table.get("redis_image", RedisService.image)),
            internal_port=int(table.get("redis_internal_port", RedisService.internal_port)),
            env_names=redis_env_names,
        )

    return DjangoContainerConfig(
        postgres=postgres,
        redis=redis,
        skip_dotenv_env=str(table.get("skip_dotenv_env", DjangoContainerConfig.skip_dotenv_env)),
        disable_env=str(table.get("disable_env", DjangoContainerConfig.disable_env)),
        reuse_env=str(table.get("reuse_env", DjangoContainerConfig.reuse_env)),
        use_django_pg_baseline=bool(table.get("use_django_pg_baseline", False)),
    )


def load_config(rootdir: Path) -> DjangoContainerConfig:
    """Load configuration. ``register()`` overrides pyproject; defaults fill gaps."""
    if _REGISTERED is not None:
        # register() expresses an override of *defaults*, not of pyproject;
        # but per SPEC §4.2 the documented behavior is "register() wins" —
        # so the registered config replaces the table wholesale.
        return _REGISTERED

    pyproject = _find_pyproject(rootdir)
    if pyproject is None:
        return DjangoContainerConfig()
    table = _load_pyproject_table(pyproject)
    return _config_from_table(table, project_root=pyproject.parent)


def apply_template_default(config: DjangoContainerConfig) -> DjangoContainerConfig:
    """SPEC §10.6: when init_scripts non-empty and template unset, default to ``database``.

    Returns a (possibly) new config; never mutates the input.
    """
    pg = config.postgres
    if pg.init_scripts and pg.template is None:
        new_pg = replace(pg, template=pg.database)
        return replace(config, postgres=new_pg)
    return config


def validate(config: DjangoContainerConfig) -> None:
    """SPEC §5.6 validation.

    Raises :class:`ConfigError` when the configuration is structurally bad.
    """
    pg = config.postgres
    required_db_keys = {"host", "port", "name", "user", "password"}
    missing = [k for k in required_db_keys if not pg.env_names.get(k)]
    if missing:
        raise ConfigError(f"PostgresService.env_names is missing required keys: {sorted(missing)}")

    for path in pg.init_scripts:
        if not path.is_file():
            raise ConfigError(f"postgres_init_scripts entry does not exist: {path}")

    if pg.template is not None and not pg.init_scripts:
        raise ConfigError(
            "postgres_template is set but postgres_init_scripts is empty — "
            "cloning an empty template DB just slows test creation. "
            "Either add init scripts or remove the template setting."
        )

    if config.redis is not None:
        redis_env = config.redis.env_names
        for key in ("host", "port"):
            if not redis_env.get(key):
                raise ConfigError(f"RedisService.env_names is missing required key: {key!r}")

    if pg.internal_port <= 0 or pg.internal_port > 65535:
        raise ConfigError(f"postgres_internal_port out of range: {pg.internal_port}")
    if config.redis is not None and (
        config.redis.internal_port <= 0 or config.redis.internal_port > 65535
    ):
        raise ConfigError(f"redis_internal_port out of range: {config.redis.internal_port}")


def is_disabled(config: DjangoContainerConfig, args: list[str]) -> bool:
    """Active by default. SPEC §6.5 step 5.

    Disabled by ``--no-testcontainers`` CLI flag or by setting
    ``config.disable_env`` to a truthy-falsy value (``1``, ``true``, ``yes``).
    """
    if "--no-testcontainers" in args:
        return True
    if not config.disable_env:
        return False
    raw = os.environ.get(config.disable_env, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def is_reuse_enabled(config: DjangoContainerConfig) -> bool:
    if not config.reuse_env:
        return False
    raw = os.environ.get(config.reuse_env, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


__all__ = [
    "apply_template_default",
    "is_disabled",
    "is_reuse_enabled",
    "load_config",
    "register",
    "validate",
]
