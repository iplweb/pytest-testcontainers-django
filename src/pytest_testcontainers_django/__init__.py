"""Bridge between ``pytest-testcontainers`` and ``pytest-django``.

The pytest plugin auto-loads via the ``pytest11`` entry point declared
in ``pyproject.toml`` — no ``-p`` flag or ``conftest.py`` import is
required.  Most users only need to interact with the public surface
re-exported here.

See ``SPEC.md`` for the full design rationale (especially §6 on the
hook-ordering timing dance).
"""

from __future__ import annotations

from pytest_testcontainers_django._types import (
    DjangoContainerConfig,
    PostgresService,
    RedisService,
)
from pytest_testcontainers_django.config import register
from pytest_testcontainers_django.errors import (
    BaselineMissingError,
    ConfigError,
    PytestTestcontainersDjangoError,
    ReuseStaleContainerError,
)

__all__ = [
    "BaselineMissingError",
    "ConfigError",
    "DjangoContainerConfig",
    "PostgresService",
    "PytestTestcontainersDjangoError",
    "RedisService",
    "ReuseStaleContainerError",
    "register",
]

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("pytest-testcontainers-django")
except PackageNotFoundError:  # pragma: no cover — only when running from a non-installed checkout
    __version__ = "0.0.0+unknown"
del PackageNotFoundError, _pkg_version
