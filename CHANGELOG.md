# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-08-07

### Changed

- **Ephemeral (non-reuse) containers are now named**
  `<project>-tc-<service>-…` like reuse containers, plus a random suffix.
  They were previously left unnamed, so Docker assigned names like
  `goofy_torvalds`: invisible to `pytest --testcontainers-clean`, which
  selects on the `<project>-tc-` prefix, and impossible to attribute to a run
  when staring at `docker ps`. Two runs of the same branch still cannot
  collide on the name.
- **The Ryuk reaper is shut down at the end of the session**, after the
  container handles are stopped. testcontainers otherwise leaves it idling for
  the remainder of its reconnection timeout. Skipped when nothing was started
  (plugin disabled, xdist worker) so those runs do not import testcontainers
  just to no-op.
- Ephemeral containers also inherit the `SIGTERM` / `SIGHUP` teardown added in
  `pytest-testcontainers` 0.3.0, so a killed run removes them immediately
  instead of leaving them to Ryuk.

### Fixed

- Requires `pytest-testcontainers >= 0.3.0`. The floor was `>= 0.2.2`, but the
  plugin now imports `shutdown_ryuk` / `ryuk_maybe_running`, which do not exist
  in earlier releases — an install resolving an older one would fail at import.
## [0.2.6] - 2026-08-07

### Added

- Support for Django 6.0 and 6.1: added the `Framework :: Django :: 6.0`
  and `:: 6.1` trove classifiers, and a dedicated `django61` tox env / CI
  job that runs the suite against `Django~=6.1.0` on Python 3.13 (Django
  6.x requires Python >=3.12). The main CI matrix stays a pytest matrix —
  Django remains a dev-only dependency of this plugin.

## [0.2.5] - 2026-06-29

### Fixed

- **`[pytest-testcontainers-django] Docker daemon is not reachable` even though
  `docker ps` worked**, on setups where the daemon lives on a non-default
  socket (OrbStack, colima, a stopped Docker Desktop). The eager-start daemon
  check used `docker.from_env()`, which — like `from_env()` everywhere — honors
  `DOCKER_HOST` but, unlike the `docker` CLI, ignores the active
  `docker context`, so it fell back to `/var/run/docker.sock` (absent or a
  dangling symlink) and crashed with `FileNotFoundError`. The plugin now
  exports `DOCKER_HOST` from the active context before any container client
  (docker-py, testcontainers, or Ryuk's socket bind-mount) is built. An
  explicit `DOCKER_HOST` still wins; with no resolvable context it falls back
  to the platform default socket.
- Import `container_name_for` instead of the removed `reuse_name_for` from
  `pytest_testcontainers.reuse`. `pytest-testcontainers` 0.2.1 renamed the
  function (it had been `reuse_name_for` through 0.2.0), which broke this
  package at import time. Bumped the `pytest-testcontainers` floor to `>=0.2.2`
  (the release that both provides `container_name_for` and re-adds a
  `reuse_name_for` compatibility alias).

## [0.2.4] - 2026-06-19

### Fixed

- The plugin now actually works on its advertised `pytest >= 7.4` floor. The
  rootdir-conftest preload (the mechanism that lets `register()` calls run
  before pytest-django's hook) called the private `_loadconftestmodules`, which
  **does not exist in pytest 7** — there the method is `_getconftestmodules`,
  with a different signature and no `consider_namespace_packages` concept. On
  pytest 7 the call raised `AttributeError`, was swallowed by the preload's
  broad `except`, and the rootdir conftest was silently never preloaded, so
  `register()`-based config and early env injection didn't take effect (6 tests
  failed). The preload now dispatches on method presence: `_loadconftestmodules`
  (with `consider_namespace_packages`) on pytest >= 8, falling back to
  `_getconftestmodules(path, importmode, rootpath)` on pytest 7.

### Added

- Test matrix across pytest 7.x, 8.x and 9.x (Python 3.10–3.13), runnable
  locally via `tox` (new `tox.ini`, `tox`/`tox-uv` added to the `dev` extra) and
  mirrored in CI. The `py3.13 × pytest7` cell is excluded (pytest gained Python
  3.13 support in 8.3). CI also splits linting into its own job.

### Changed

- Relaxed the pytest dependency cap from `<9` to `<10`, so the package installs
  alongside pytest 9 (validated by the new matrix). Bumped the
  `pytest-testcontainers` floor to `>= 0.2.0`, the release whose own pytest cap
  was raised to `<10` (0.1.0 pinned `pytest < 9` and would otherwise block
  pytest 9 transitively).

## [0.2.3] - 2026-06-01

### Fixed

- The rootdir-conftest preload no longer dumps a spurious traceback
  (`preloading rootdir conftest.py raised; continuing`) when the conftest
  transitively touches the Django app registry before `django.setup()` has
  run — e.g. `model_bakery` >= 1.20 calling `apps.is_installed()` at import.
  0.2.1 added a quiet-deferral shortcut, but it only recognised
  `ImproperlyConfigured` (raised when `DJANGO_SETTINGS_MODULE` is unset).
  When the settings module *is* configured early but the app registry has
  not been populated yet, Django raises the sibling exception
  `AppRegistryNotReady` ("Apps aren't loaded yet.") instead — which is *not*
  a subclass of `ImproperlyConfigured`, so the shortcut missed it and the
  benign deferral was logged as an error. The predicate (renamed
  `_is_django_not_configured` → `_is_django_not_ready`) now matches both
  exceptions by class name.

## [0.2.2] - 2026-05-14

### Fixed

- Serial (no-`-n`) pytest runs no longer connect to the developer's local
  Postgres port instead of the testcontainer port.  Root cause: the
  rootdir-conftest preload (the mechanism that lets `register()` calls
  run before pytest-django's hook) can transitively touch
  `django.conf.settings` at module top level — e.g. a BPP-style
  `from django.utils.translation import activate` in `conftest.py`.  That
  triggers Django's lazy `Settings` to bind **before** we inject the
  container's host/port into `os.environ`, freezing
  `DATABASES["default"]["PORT"]` to whatever the developer's `.env` or
  shell had set (commonly `5432`).  Pytest-django's later `django.setup()`
  reused that stale cache, so tests opened psycopg connections to the
  wrong port.  pytest-xdist runs accidentally hid the bug because
  workers are fresh subprocesses that re-import everything against the
  injected env.  Fix: after `inject()` succeeds, evict the cached
  `LazySettings._wrapped` and drop the user's settings sub-package from
  `sys.modules`, so the next access (pytest-django's `django.setup()`)
  re-imports module-level code with the now-corrected environment.

## [0.2.1] - 2026-05-09

### Changed

- The rootdir-conftest preload (the mechanism that lets `register()` calls
  run before pytest-django's hook) now logs at DEBUG level — instead of
  emitting a full WARNING-level traceback — when the import fails because
  Django settings aren't configured yet.  This is the common case when a
  conftest transitively imports `model_bakery >= 1.20`, which calls
  `apps.is_installed(...)` at module import time.  The conftest is still
  re-imported by pytest's normal trylast loader after our env injection
  and pytest-django run, so tests work as before — only the noisy
  traceback at the end of the run is gone.  Unrelated conftest errors
  (anything not chaining to `ImproperlyConfigured`) keep the original
  WARNING + traceback behaviour.

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
