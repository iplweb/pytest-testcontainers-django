"""Public exception classes."""

from __future__ import annotations


class PytestTestcontainersDjangoError(Exception):
    """Base class for all errors raised by this plugin."""


class ConfigError(PytestTestcontainersDjangoError):
    """Raised when configuration is invalid (SPEC §5.6)."""


class BaselineMissingError(PytestTestcontainersDjangoError):
    """Raised when ``use_django_pg_baseline = true`` but ``django-pg-baseline`` is unavailable."""


__all__ = [
    "BaselineMissingError",
    "ConfigError",
    "PytestTestcontainersDjangoError",
]
