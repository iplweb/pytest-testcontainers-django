"""Public exception classes."""

from __future__ import annotations


class PytestTestcontainersDjangoError(Exception):
    """Base class for all errors raised by this plugin."""


class ConfigError(PytestTestcontainersDjangoError):
    """Raised when configuration is invalid (SPEC §5.6)."""


class BaselineMissingError(PytestTestcontainersDjangoError):
    """Raised when ``use_django_pg_baseline = true`` but ``django-pg-baseline`` is unavailable."""


class ReuseStaleContainerError(PytestTestcontainersDjangoError):
    """Raised when reuse mode finds a pre-existing container that can't be revived
    (e.g. status ``dead``/``removing``).  Starting a fresh container with the same
    name would fail with a Docker name conflict, so we surface a clear actionable
    error instead.
    """


__all__ = [
    "BaselineMissingError",
    "ConfigError",
    "PytestTestcontainersDjangoError",
    "ReuseStaleContainerError",
]
