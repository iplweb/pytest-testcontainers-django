# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-05-08

Initial release.

### Added

- `pytest_load_initial_conftests(tryfirst=True)` hook that starts Postgres
  (and optionally Redis) containers and injects connection details into
  `os.environ` **before** pytest-django imports settings.
- Configuration via `[tool.pytest-testcontainers-django]` in `pyproject.toml`
  or programmatically via `register(DjangoContainerConfig(...))` from
  `conftest.py`.
- Postgres init-script mounting (`/docker-entrypoint-initdb.d/NN-name.sql`)
  with automatic `postgres_template = postgres_database` defaulting when
  init scripts are present (SPEC §10.6).
- Reuse mode via `PYTEST_TESTCONTAINERS_REUSE=1`, with a stderr warning
  when init scripts wouldn't be replayed against a pre-existing container
  (SPEC §10.7).
- pytest-xdist worker handling: workers inherit env from the controller and
  only set the `*_SKIP_DOTENV` flag (SPEC §7).
- Optional integration with `django-pg-baseline` via the
  `use_django_pg_baseline = true` flag.
- atexit safety net for abrupt-exit paths that skip `pytest_unconfigure`.
- Custom `*_SKIP_DOTENV` env-var injection so projects using django-environ
  don't have their just-injected ports clobbered by `.env` reload (SPEC §9).
