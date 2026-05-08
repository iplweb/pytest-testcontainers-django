# pytest-testcontainers-django

[![CI](https://github.com/iplweb/pytest-testcontainers-django/actions/workflows/ci.yml/badge.svg)](https://github.com/iplweb/pytest-testcontainers-django/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pytest-testcontainers-django.svg)](https://pypi.org/project/pytest-testcontainers-django/)
[![Python versions](https://img.shields.io/pypi/pyversions/pytest-testcontainers-django.svg)](https://pypi.org/project/pytest-testcontainers-django/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Bridge between [`pytest-testcontainers`][pt] and [`pytest-django`][pd]:
starts a Postgres (and optionally Redis) container **before** Django imports
its settings, so your tests run against a real, ephemeral DB without any
docker-compose orchestration — and without "Connection refused" against
port 5432 because Django read `os.environ` too early.

[pt]: https://github.com/iplweb/pytest-testcontainers
[pd]: https://github.com/pytest-dev/pytest-django

## Why this package exists

Django evaluates `DATABASES` at **module-import time**.  pytest-django
imports settings during its `pytest_load_initial_conftests` hook.  Any
fixture-based testcontainer setup runs *after* that — so by the time the
container has a port, Django has already opened a connection (or failed
to) against whatever your `.env` had at pytest startup.

The only correct hook for "start a container, write its port to
`os.environ`, before Django imports settings" is
`pytest_load_initial_conftests` itself, registered with
`@pytest.hookimpl(tryfirst=True)`.  That single detail is the core IP of
this package; the rest is plumbing — xdist worker propagation, dotenv
suppression, init-script mounting, TEST TEMPLATE wiring, cleanup
ordering.

See [`SPEC.md`](SPEC.md) for the full design rationale (especially §6 on
the timing dance).

## Features

- `pytest_load_initial_conftests(tryfirst=True)` hook that runs **before**
  pytest-django imports your `settings.py`.
- Zero-config defaults — works out of the box for projects whose
  `settings.py` reads `DJANGO_DB_HOST` / `DJANGO_DB_PORT` / etc. from
  `os.environ`.
- Declarative configuration in `[tool.pytest-testcontainers-django]` or
  programmatic configuration via `register(DjangoContainerConfig(...))`
  from `conftest.py`.
- Postgres init-script mounting (`/docker-entrypoint-initdb.d/`) with
  automatic `DATABASES['TEST']['TEMPLATE']` defaulting — so
  `pytest --create-db` finishes in seconds.
- Optional Redis container with the same timing-safe injection pattern.
- pytest-xdist support — workers inherit the controller's environment,
  no port-fight.
- `--no-testcontainers` / `PYTEST_TESTCONTAINERS_DISABLE=1` to delegate
  to docker-compose; `PYTEST_TESTCONTAINERS_REUSE=1` for fast local
  iteration.
- atexit safety net for abrupt-exit paths that skip `pytest_unconfigure`.
- Optional integration with [`django-pg-baseline`](https://github.com/iplweb/django-pg-baseline)
  for managed baseline SQL artifacts.

## Supported versions

### Python

| Python | 3.10 | 3.11 | 3.12 | 3.13 |
|--------|------|------|------|------|
|        | ✓    | ✓    | ✓    | ✓    |

### Django

Authoritative upstream: <https://docs.djangoproject.com/en/dev/faq/install/#what-python-version-can-i-use-with-django>

| Django  | 3.10 | 3.11 | 3.12 | 3.13 | Status                       |
|---------|------|------|------|------|------------------------------|
| 4.2 LTS | ✓    | ✓    | ✓    | —    | Extended support to Apr 2026 |
| 5.2 LTS | ✓    | ✓    | ✓    | ✓    | Active LTS                   |

EOL Django releases (5.0, 5.1) are not listed but should still work —
this package only consumes pytest-django's hook surface, not Django
internals.  Open an issue if you need an LTS-only reassurance.

## Install

### Using uv (recommended)

```bash
uv add pytest-testcontainers-django
```

### Using pip

```bash
pip install pytest-testcontainers-django
```

You also need a working Docker daemon on the host running pytest.  No
extra system libraries are required — the package is pure Python.

## Quick start

For most projects, configuration lives in `pyproject.toml`.  Zero
`conftest.py` needed:

```toml
[tool.pytest-testcontainers-django]
postgres_image = "postgres:16"
postgres_user = "myapp"
postgres_password = "myapp"
postgres_database = "myapp"

# Env-var names this plugin writes into os.environ.
# These are the same names your settings.py reads.
db_host_env = "DJANGO_DB_HOST"
db_port_env = "DJANGO_DB_PORT"
db_name_env = "DJANGO_DB_NAME"
db_user_env = "DJANGO_DB_USER"
db_password_env = "DJANGO_DB_PASSWORD"
db_test_template_env = "DJANGO_DB_TEST_TEMPLATE"
skip_dotenv_env = "DJANGO_SKIP_DOTENV"
```

Your `settings.py` reads these env vars exactly as you'd expect:

```python
import environ
import os

env = environ.Env()

# Skip .env loading when the plugin already populated os.environ —
# otherwise read_env(overwrite=True) would clobber our injected port.
if not os.environ.get("DJANGO_SKIP_DOTENV"):
    environ.Env.read_env(".env", overwrite=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DJANGO_DB_NAME"),
        "USER": env("DJANGO_DB_USER"),
        "PASSWORD": env("DJANGO_DB_PASSWORD"),
        "HOST": env("DJANGO_DB_HOST"),
        "PORT": env("DJANGO_DB_PORT"),
    },
}

# Wire the TEST TEMPLATE env var (optional but recommended when you
# load init scripts — see "Init scripts / baseline" below).
_test_template = env("DJANGO_DB_TEST_TEMPLATE", default="")
if _test_template:
    DATABASES["default"]["TEST"] = {"TEMPLATE": _test_template}
```

That's it — `pytest` will start a Postgres container, inject the port,
let pytest-django import settings, and tear the container down at exit.

## Init scripts / baseline

Mount SQL files into the Postgres container's
`/docker-entrypoint-initdb.d/` so they're replayed once on cluster init —
significantly faster than running `psql -f` from the host:

```toml
[tool.pytest-testcontainers-django]
postgres_database = "myapp"
postgres_init_scripts = [
    "tests/fixtures/baseline.sql",
    "tests/fixtures/extensions.sql",
]
# postgres_template defaults to postgres_database when init_scripts is
# set, so the test DB will be created via fast in-server CREATE DATABASE
# test_<X> WITH TEMPLATE myapp instead of replaying migrations.
```

Combine with the `DATABASES['default']['TEST']['TEMPLATE']` snippet
above to make `pytest --create-db` finish in seconds.

## Optional Redis

```toml
[tool.pytest-testcontainers-django]
redis_enabled = true
redis_image = "redis:7-alpine"
redis_host_env = "DJANGO_REDIS_HOST"
redis_port_env = "DJANGO_REDIS_PORT"
```

Your settings reads `DJANGO_REDIS_HOST` / `DJANGO_REDIS_PORT` and folds
them into a `redis://...` URL however your stack prefers.

## Programmatic configuration

For projects that need conditional configuration or want to wire in
[`django-pg-baseline`](https://github.com/iplweb/django-pg-baseline) for
baseline-managed seed data, register from `conftest.py`:

```python
# conftest.py at the project root
from pathlib import Path

from pytest_testcontainers_django import (
    DjangoContainerConfig,
    PostgresService,
    RedisService,
    register,
)

register(
    DjangoContainerConfig(
        postgres=PostgresService(
            image="postgres:16",
            user="myapp",
            password="myapp",
            database="myapp",
            init_scripts=[Path("tests/fixtures/baseline.sql")],
            template="myapp",
        ),
        redis=RedisService(),
    )
)
```

`register()` overrides any `pyproject.toml` table.  This works because
the plugin force-imports the rootdir `conftest.py` from inside its
`tryfirst` hook, so top-level `register(...)` calls run before
configuration is read.

## Disable / reuse

```bash
# Delegate to docker-compose / pre-existing services:
pytest --no-testcontainers
PYTEST_TESTCONTAINERS_DISABLE=1 pytest

# Keep containers alive between runs for fast local iteration:
PYTEST_TESTCONTAINERS_REUSE=1 pytest
```

## pytest-xdist

Workers inherit the controller's environment on fork, so they don't
start new containers — they only re-set the `*_SKIP_DOTENV` flag so
django-environ doesn't re-read `.env` on settings re-import.

## Coexistence with other testcontainers

Django projects that need additional services (Elasticsearch, MinIO,
Kafka, etc.) declare plain pytest fixtures using
`pytest-testcontainers`'s maker functions directly — no special
integration with this package needed:

```python
# conftest.py
import pytest
from pytest_testcontainers import make_container

@pytest.fixture(scope="session")
def minio():
    with make_container("minio/minio:latest", ports={"9000/tcp": None}) as c:
        yield c
```

Late resolution is fine for non-DB services — their host:port is read
at *connection time*, not import time.  Only Django's `DATABASES` has
the import-time-read race that this package solves.

## Configuration reference

| Pyproject key                 | Default                          | Purpose                                          |
| ----------------------------- | -------------------------------- | ------------------------------------------------ |
| `postgres_image`              | `postgres:16`                    | Image used for the DB container                  |
| `postgres_user`               | `postgres`                       | `POSTGRES_USER`                                  |
| `postgres_password`           | `postgres`                       | `POSTGRES_PASSWORD`                              |
| `postgres_database`           | `postgres`                       | `POSTGRES_DB`                                    |
| `postgres_internal_port`      | `5432`                           | Image's internal port                            |
| `postgres_template`           | (= `postgres_database` when init scripts set, else unset) | Value injected as `DATABASES['TEST']['TEMPLATE']` |
| `postgres_init_scripts`       | `[]`                             | Paths mounted into `/docker-entrypoint-initdb.d/`|
| `postgres_env`                | `{}`                             | Image-specific env (e.g. tuning flags)           |
| `db_host_env`                 | `DJANGO_DB_HOST`                 | Env var written with the resolved host           |
| `db_port_env`                 | `DJANGO_DB_PORT`                 | Env var written with the resolved port           |
| `db_name_env`                 | `DJANGO_DB_NAME`                 | Env var written with `postgres_database`         |
| `db_user_env`                 | `DJANGO_DB_USER`                 | Env var written with `postgres_user`             |
| `db_password_env`             | `DJANGO_DB_PASSWORD`             | Env var written with `postgres_password`         |
| `db_test_template_env`        | `DJANGO_DB_TEST_TEMPLATE`        | Env var written with `postgres_template`         |
| `skip_dotenv_env`             | `DJANGO_SKIP_DOTENV`             | Env var your settings checks before reading .env |
| `disable_env`                 | `PYTEST_TESTCONTAINERS_DISABLE`  | Env var that disables the plugin                 |
| `reuse_env`                   | `PYTEST_TESTCONTAINERS_REUSE`    | Env var that enables reuse mode                  |
| `redis_enabled`               | `false`                          | Enable Redis                                     |
| `redis_image`                 | `redis:7-alpine`                 | Image used for Redis                             |
| `redis_internal_port`         | `6379`                           |                                                  |
| `redis_host_env`              | `DJANGO_REDIS_HOST`              |                                                  |
| `redis_port_env`              | `DJANGO_REDIS_PORT`              |                                                  |
| `use_django_pg_baseline`      | `false`                          | Auto-prepend `django-pg-baseline`'s artifact     |

## License

MIT — see [`LICENSE`](LICENSE).
