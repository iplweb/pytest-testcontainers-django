"""Public dataclasses used for programmatic configuration via :func:`register`.

See SPEC §4.2 / §5. Default env-var names follow the ``DJANGO_DB_*`` /
``DJANGO_REDIS_*`` prefix idiom (SPEC §5.1.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB_ENV_NAMES: dict[str, str] = {
    "host": "DJANGO_DB_HOST",
    "port": "DJANGO_DB_PORT",
    "name": "DJANGO_DB_NAME",
    "user": "DJANGO_DB_USER",
    "password": "DJANGO_DB_PASSWORD",
    "test_template": "DJANGO_DB_TEST_TEMPLATE",
}

DEFAULT_REDIS_ENV_NAMES: dict[str, str] = {
    "host": "DJANGO_REDIS_HOST",
    "port": "DJANGO_REDIS_PORT",
}


@dataclass
class PostgresService:
    image: str = "postgres:16"
    user: str = "postgres"
    password: str = "postgres"
    database: str = "postgres"
    internal_port: int = 5432
    template: str | None = None
    init_scripts: list[Path] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    env_names: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DB_ENV_NAMES))


@dataclass
class RedisService:
    image: str = "redis:7-alpine"
    internal_port: int = 6379
    env_names: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_REDIS_ENV_NAMES))


@dataclass
class DjangoContainerConfig:
    postgres: PostgresService = field(default_factory=PostgresService)
    redis: RedisService | None = None
    skip_dotenv_env: str = "DJANGO_SKIP_DOTENV"
    disable_env: str = "PYTEST_TESTCONTAINERS_DISABLE"
    reuse_env: str = "PYTEST_TESTCONTAINERS_REUSE"
    use_django_pg_baseline: bool = False


__all__ = [
    "DEFAULT_DB_ENV_NAMES",
    "DEFAULT_REDIS_ENV_NAMES",
    "DjangoContainerConfig",
    "PostgresService",
    "RedisService",
]
