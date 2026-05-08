# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-05-08

### Added

- New `[redis]` install extra so `redis_enabled = true` works without a
  separate `pip install redis` (`testcontainers.redis` imports the Python
  redis client at module load).
- `ReuseStaleContainerError` and matching `pytest.UsageError` for the
  reuse-mode edge case where a pre-existing container is in `dead` /
  `removing` state — surfaced with the exact `docker rm -f <name>`
  command instead of letting Docker fail the start with a 409 name
  conflict.

### Changed

- `__version__` now reads from `importlib.metadata` so it stays in sync
  with `pyproject.toml`.
- README Django support matrix marks 4.2 LTS as past EOL (Apr 2026).

### Fixed

- The plugin's own test suite now disables eager-start via a *root*
  `conftest.py` rather than `tests/conftest.py`.  The root file is
  preloaded by the plugin's `tryfirst` hook; `tests/conftest.py` is not
  — meaning the previous setup silently ran every "unit" test against
  a real Docker daemon when one was available locally (CI was unaffected
  because it sets the env var explicitly).

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
