# pytest-testcontainers-django — SPEC

Status: draft / pre-implementation
Audience: maintainers extracting this package out of BPP
            (`/Users/mpasternak/Programowanie/bpp`); future contributors.
Companion packages:
- `pytest-testcontainers` (#1, **purely fixture-based** generic container
  primitives — maker functions, hybrid patterns. This package depends on
  it for the container-start primitives, **not** for any pytest hook.)
- `django-pg-baseline` (#3, manages a `baseline.sql` artifact, this
  package optionally consumes its output via `get_baseline_path()`.)

---

## 1. Purpose

Bridge between `pytest-testcontainers` (#1) and `pytest-django`. Solves
a single, concrete, ugly timing problem that every Django project trying
to use testcontainers eventually hits:

> Django reads its `DATABASES` config from `os.environ` at **import
> time**. Pytest-django imports settings during its own
> `pytest_load_initial_conftests` hook. Any fixture that starts a
> container — including session-scoped, autouse fixtures — runs **after**
> pytest-django imports settings. By the time the test container has a
> port, Django already opened a connection (or failed to) against
> whatever was in `.env` when pytest started.

The only correct hook for "start a container, write its port to
`os.environ`, before Django imports settings" is `pytest_load_initial_conftests`
itself, registered with `@pytest.hookimpl(tryfirst=True)`. **That single
detail is the core IP of this package.** The rest is plumbing around
it: xdist worker propagation, dotenv suppression, init-script mounting,
TEST TEMPLATE setting, cleanup ordering.

The canonical write-up of this race condition lives today in BPP at
`src/testcontainers_bpp/plugin.py`, lines 63–92. Direct quote (Polish
preserved, as it's part of the historical record of why this package
exists):

> Pytest-django ma własny hook `pytest_load_initial_conftests`, w
> którym wymusza załadowanie ustawień Django (`dj_settings.DATABASES`,
> `pytest_django/plugin.py:357`). To powoduje import modułu
> `django_bpp.settings.base`, który czyta parametry połączenia z
> `os.environ` w momencie importu — np. `env("DJANGO_BPP_DB_PORT")`
> w linii 679.
>
> Deweloper zazwyczaj ma w shellu (przez direnv / `.env`)
> `DJANGO_BPP_DB_PORT=5432` — domyślny port docker-compose. Jeżeli
> Django załaduje ustawienia ZANIM ta funkcja ustawi porty z
> testcontainerów, to Django połączy się z portem 5432 (albo dostanie
> „Connection refused" gdy docker-compose nie działa). Dynamiczny
> port z testcontainerów (np. 63403) nigdy nie dotrze do Django.

Translation of intent: this gap between `os.environ` snapshot and
Django settings import is the root cause. There is no fixture-level fix.
The solution is hook-level + `tryfirst`.

This package exists so other Django projects don't have to rediscover
that detail by debugging "Connection refused" against the wrong port
for two evenings.

### 1.1 Architectural split with #1

After several iterations, the boundary between #1 and #2 settled on
**purely fixture-based** for #1 and **eager-start hook** for #2:

- **#1 (`pytest-testcontainers`)** is a generic, framework-agnostic
  helper package. It exposes maker functions (`make_postgres`,
  `make_redis`, `make_minio`, …) and hybrid patterns for users who want
  to wire up containers as ordinary pytest fixtures (session-scoped,
  module-scoped, function-scoped — their choice). **It registers no
  pytest hooks. It reads no project-level config files. It does
  nothing at session start.** Non-Django projects use #1 standalone.
- **#2 (this package)** is the Django glue layer. It owns the
  `pytest_load_initial_conftests(tryfirst=True)` hook, the env-var
  injection, the dotenv-skip flag, the TEST TEMPLATE wiring, the
  init-scripts mount contract, and xdist worker propagation. It
  imports `make_postgres` / `make_redis` from #1 to actually
  instantiate containers, and that's the entire dependency surface
  upward.

Why this split? Because the eager-start machinery is **Django-specific**.
No other framework reads `DATABASES` at module import time the way Django
does. `pytest-testcontainers` (#1) staying purely fixture-based keeps
it useful for FastAPI, Flask, Pyramid, plain library testing, and any
other context where "fixtures resolve at test time" is the natural
contract. Hauling the eager-start engine into #1 would force every
non-Django user to think about hook ordering they don't need.

---

## 2. Scope

### In scope

- `pytest_load_initial_conftests(tryfirst=True)` implementation that:
  1. Reads project config (pyproject.toml table or `register()` call).
  2. Calls #1's maker functions (`make_postgres(...)`, `make_redis(...)`)
     to start containers on the controller process.
  3. Injects connection details into `os.environ` under
     project-configured names.
  4. Sets a `*_TEST_TEMPLATE` env var so Django creates the test DB
     by `CREATE DATABASE … WITH TEMPLATE`.
  5. Sets a `*_SKIP_DOTENV` flag so django-environ doesn't clobber
     injected values.
  6. Detects pytest-xdist workers and skips re-starting containers
     (workers inherit env vars from the controller).
- A small configuration model (env-var name mapping, image, credentials,
  init scripts, TEMPLATE setting) declared in `pyproject.toml` and/or
  registered from a project's `conftest.py`.
- Optional Redis support via the same configuration model.
- `--no-testcontainers` CLI flag and an env-var equivalent for
  disabling the plugin (delegating to docker-compose / pre-existing
  services).
- Optional `BPP_TESTCONTAINERS_REUSE`-style reuse mode for fast local
  iteration (delegated to #1's reuse semantics).
- atexit safety net for graceful pytest-process exits.
- Helper for consuming a SQL file from #3 (mount → init scripts →
  TEMPLATE).

### Out of scope

- Generic "start any container" lifecycle. That is #1's job. This
  package only knows about Django's contract with the container.
- Fixture-based container lifecycles for arbitrary services. That is
  #1's job. If a user wants `tc_minio` per-session, they declare a
  fixture using #1's `make_minio` directly. We don't proxy.
- Managing the SQL artifact itself (creation, refresh, version
  pinning). That is #3's job.
- Replacing pytest-django. We integrate with it; we do not subsume it.
- Provisioning Docker on host machines, Docker-in-Docker tricks, CI
  service-container detection. The user is responsible for a working
  Docker daemon.
- Supporting non-pytest-django setups (e.g. Django's stock
  `manage.py test`). Hook is pytest-only.
- Fixturized container lifecycle for the **DB and Redis services we
  manage**. Containers in this package are always **process-scoped**,
  started in `pytest_load_initial_conftests`, stopped in
  `pytest_unconfigure` + atexit. Fixture-level scopes are too late
  for the timing dance — see §6.

---

## 3. Dependency on `pytest-testcontainers` (#1)

This is a **narrow, surgical** dependency. After the architectural
split (see §1.1), #2 consumes only the container-start primitives
from #1 — nothing else.

### 3.1 What we import from #1

```python
from pytest_testcontainers import (
    DockerNotRunningError,
    make_postgres,         # spec → started PostgresContainer handle
    make_redis,            # spec → started RedisContainer handle
)
```

That's it. Two maker functions and one exception class. The maker
functions are synchronous calls that:

- Pull the image if missing.
- Start the container with the requested env, ports, mounts.
- Wait for the readiness signal (TCP for Postgres, PING for Redis).
- Return a handle exposing `.host`, `.port`, `.stop()` and image-specific
  helpers (e.g. `.connection_url()`).

### 3.2 What we explicitly DO NOT use from #1

- **No fixtures**. We never `pytest.fixture`-decorate anything from
  #1's surface. #1's session-scoped fixtures (`tc_postgres`,
  `tc_redis`) resolve too late — by the time the first test asks for
  them, pytest-django has already imported settings.
- **No pytest hooks**. #1 ships no hooks under the new architecture.
  Even if it did, we'd not use them: the timing contract (`tryfirst`,
  ordering against pytest-django) is #2's concern alone.
- **No env-var injection from #1**. #1's maker functions return
  handles. Anything that touches `os.environ` is #2's job.
- **No reading of `[tool.pytest-testcontainers]` config from #1**.
  #1 has no controller-level pyproject reader. Our config table is
  `[tool.pytest-testcontainers-django]` and we read it ourselves.

### 3.3 Why this matters

The eager-start machinery (hook, env injection, dotenv suppression,
xdist worker propagation, init-scripts mounting, TEST TEMPLATE wiring)
**lives entirely in #2**. Non-Django users never import #2 and never
pay the cost of the timing dance.

Django projects that need **additional** services (Elasticsearch,
MinIO, Kafka) beyond eager-started DB+Redis declare plain pytest
fixtures using #1's makers. Late resolution is fine for those —
their host:port is read at *connection time*, not import time. Only
Django's `DATABASES` has the import-time-read race.

```python
# project conftest.py — coexistence pattern
import pytest
from pytest_testcontainers import make_minio  # #1's maker

@pytest.fixture(scope="session")
def minio():
    container = make_minio(image="minio/minio:latest")
    yield container
    container.stop()
```

### 3.4 Version pinning

#2 pins #1 to a compatible major (`pytest-testcontainers>=1,<2`).
Independent release cadence; breaking changes to #1's maker signatures
require coordinated release.

---

## 4. Public API

### 4.1 Default mode — declarative configuration in `pyproject.toml`

This is the path most projects should take. Zero conftest.py needed.

```toml
[tool.pytest-testcontainers-django]
# Postgres container.
postgres_image = "postgres:16"
postgres_user = "postgres"
postgres_password = "postgres"
postgres_database = "postgres"
postgres_internal_port = 5432

# Optional: image-specific knobs forwarded as container env.
postgres_env = { POSTGRESQL_UNSAFE_BUT_FAST = "1" }

# Optional: load SQL files as init scripts. Paths are interpreted
# relative to the project root (where pyproject lives).
postgres_init_scripts = ["tests/fixtures/baseline.sql"]

# Optional: name of DB after init scripts ran; used as the source for
# CREATE DATABASE test_<name> WITH TEMPLATE <postgres_template>.
postgres_template = "postgres"

# Env-var name mapping. These are the names #2 will write into
# os.environ. Project's settings.py reads from these same names.
db_host_env = "DJANGO_DB_HOST"
db_port_env = "DJANGO_DB_PORT"
db_name_env = "DJANGO_DB_NAME"
db_user_env = "DJANGO_DB_USER"
db_password_env = "DJANGO_DB_PASSWORD"
db_test_template_env = "DJANGO_DB_TEST_TEMPLATE"
skip_dotenv_env = "DJANGO_SKIP_DOTENV"

# Optional: Redis.
redis_enabled = false
redis_image = "redis:7-alpine"
redis_internal_port = 6379
redis_host_env = "DJANGO_REDIS_HOST"
redis_port_env = "DJANGO_REDIS_PORT"

# Disable / reuse switches (env-var names that the user sets,
# not values).
disable_env = "PYTEST_TESTCONTAINERS_DISABLE"
reuse_env = "PYTEST_TESTCONTAINERS_REUSE"
```

Plus the always-available CLI flag `--no-testcontainers`.

### 4.2 Programmatic mode — `conftest.py` registration

For projects that need conditional configuration, dynamic image
selection, or want to wire #3 in directly:

```python
# conftest.py at project root
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
            env={"POSTGRESQL_UNSAFE_BUT_FAST": "1"},
            env_names={
                "host": "MYAPP_DB_HOST",
                "port": "MYAPP_DB_PORT",
                "name": "MYAPP_DB_NAME",
                "user": "MYAPP_DB_USER",
                "password": "MYAPP_DB_PASSWORD",
                "test_template": "MYAPP_DB_TEST_TEMPLATE",
            },
        ),
        redis=RedisService(
            image="redis:7-alpine",
            env_names={
                "host": "MYAPP_REDIS_HOST",
                "port": "MYAPP_REDIS_PORT",
            },
        ),
        skip_dotenv_env="MYAPP_SKIP_DOTENV",
    )
)
```

`register()` is callable from a `conftest.py` at module top level —
that file is imported during pytest's bootstrap, **before** the
`pytest_load_initial_conftests` hook runs, so the registered config
is available when the hook fires. (See §6 for the ordering proof.)

If both `pyproject.toml` and `register()` are present, `register()`
wins — predictable for the "I needed to override one thing dynamically"
case.

### 4.3 BPP after migration (sketch)

BPP today hardcodes everything BPP-specific in `plugin.py` (env names,
image, credentials, template DB). After migration, BPP's
`pyproject.toml` carries the configuration via the keys above —
`postgres_image = "iplweb/bpp_dbserver:psql-16.13"`,
`postgres_user/password/database = "bpp"/"password"/"bpp"`,
`db_*_env = "DJANGO_BPP_DB_*"`, `skip_dotenv_env = "DJANGO_BPP_SKIP_DOTENV"`,
`disable_env = "BPP_USE_TESTCONTAINERS"`, `reuse_env =
"BPP_TESTCONTAINERS_REUSE"`. See §13 for the full BPP→generic mapping.

Plus a one-line `register()` hook in `conftest.py` that resolves
`baseline.sql`'s location through #3 — see §10.

---

## 5. Configuration model

### 5.1 Resolution order

For each setting:

1. `register()` value if `conftest.py` called it (highest priority).
2. `[tool.pytest-testcontainers-django]` table in nearest `pyproject.toml`
   walking up from rootdir.
3. Built-in defaults (sensible: `postgres:16`, env var names with the
   `DJANGO_DB_*` / `DJANGO_REDIS_*` / `DJANGO_DB_TEST_TEMPLATE` /
   `DJANGO_SKIP_DOTENV` prefix).

### 5.2 Required vs. optional

Required — none. Zero-config invocation must work for a project that
has `DATABASES['default']['HOST']` reading from `DJANGO_DB_HOST` etc.,
with default Postgres credentials. Realistic defaults beat documentation.

Optional — everything else (custom env-var names, custom image,
init scripts, Redis, reuse, disable).

### 5.3 Environment-variable name mapping

This is the single most important configuration concept. Different
projects use different prefixes:

| Project                | Prefix             |
| ---------------------- | ------------------ |
| BPP                    | `DJANGO_BPP_*`     |
| Django default tutorial| `DJANGO_*`         |
| 12-factor / Heroku     | `DATABASE_URL`     |
| django-environ idiom   | `DATABASE_URL` or  |
|                        | individual `DB_*`  |

We support the **individual env-var** style natively. `DATABASE_URL`
parsing is **out of scope** for v1 — it's a separate concern (the
url-format itself is fine, but injecting "compose a URL from a dynamic
port" through `os.environ["DATABASE_URL"]` is just as easy from the
caller side and we'd inherit the URL-parser bug surface). See §13
for revisit-later notes.

### 5.4 `postgres_env`

Pass-through for image-specific knobs. BPP sets
`POSTGRESQL_UNSAFE_BUT_FAST=1` and
`POSTGRESQL_MAX_LOCKS_PER_TRANSACTION=512` on its custom
`iplweb/bpp_dbserver` image. This is opaque to us — we just forward it
to #1's `make_postgres(env=...)` parameter.

### 5.5 `postgres_internal_port` / `redis_internal_port`

Most images use 5432 / 6379. Some don't (Postgres images sometimes
expose 15432, custom-built images do as they please). Configurable;
defaults match upstream official images.

### 5.6 Validation

At hook time, before starting containers:

- Reject combinations like `redis_enabled=true` with no
  `redis_host_env` / `redis_port_env`.
- Reject `init_scripts` paths that don't exist (fail loudly: don't
  silently fall back to "no init", that's the BPP `find_baseline_sql`
  warning that nobody reads).
- Reject `postgres_template` set without any `postgres_init_scripts` —
  TEMPLATE pointing at an empty template DB just slows down test DB
  creation for no reason.

---

## 6. The timing dance

This section is the **educational core** of the package. Anyone reading
the source should leave understanding why the hook ordering matters.
The full explanation lives **here**, not in #1's spec — #1 is purely
fixture-based and never deals with this race.

### 6.1 Why this is Django-specific

Django settings are evaluated at module-import time. A typical
`settings.py` has top-level code like:

```python
import environ
env = environ.Env()
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
```

When `django.setup()` runs, it imports the settings module. Every
`env(...)` call resolves against `os.environ` **at that moment**. After
import, the resolved values are baked into `settings.DATABASES`.
Mutating `os.environ` afterward changes nothing for that connection.
Worse, third-party Django apps imported during settings load can
themselves read `settings.DATABASES['default']['HOST']` immediately
(SSL options blocks, connection pool init, schema introspection) and
**cache** that value — even a post-setup `settings.DATABASES['default']
['HOST'] = ...` mutation won't reach them.

Other frameworks don't have this property. FastAPI, Flask, Pyramid all
read DB config at *application startup* or at *connection time*. If
your testcontainer port is available by the time the app starts (or
the first request is served), you're fine. Their config models are
re-bindable.

This is why fixtures work for non-Django projects but not for Django:
the import-time read happens before any fixture runs.

### 6.2 Why fixtures don't work here

A fixture — even session-scoped, even autouse — resolves only when a
test (or another fixture) requests it. The fixture machinery itself
runs inside `pytest_collection` and `pytest_runtest_setup`, both of
which fire **after** `pytest_configure`, which fires **after**
`pytest_load_initial_conftests`. By the time any fixture body executes,
pytest-django has already done `django.setup()` and Django has already
read `os.environ`.

Concretely, here's what a session-scoped autouse fixture would look
like, and why it fails:

```python
# does NOT work — too late
@pytest.fixture(scope="session", autouse=True)
def _start_pg():
    pg = make_postgres(...)
    os.environ["DJANGO_DB_PORT"] = str(pg.port)
    yield pg
    pg.stop()
```

By the time `_start_pg` runs, `connection.settings_dict["PORT"]` is
already the wrong port (whatever was in `.env` at pytest startup,
typically `5432`). Setting `os.environ` here is dead code from Django's
perspective — Django doesn't re-read it.

The only place a hook can run before `django.setup()` is itself called
inside a hook fired earlier than pytest-django's
`pytest_load_initial_conftests`. The earliest such hook is
`pytest_load_initial_conftests` itself, registered with `tryfirst=True`.

### 6.3 The race

When `pytest` starts with `--ds=myapp.settings`, the following sequence
fires (simplified, but accurate to pytest 7+ and pytest-django 4+):

1. `pytest` parses CLI args and discovers plugins (entry points,
   `-p` flags, `conftest.py` imports near rootdir).
2. `pytest_cmdline_main` runs. Conftest plugins (registered via
   `entry_points` in `pyproject.toml`) are loaded.
3. **`pytest_load_initial_conftests` hooks fire**, in pluggy order
   (tryfirst → default → trylast).
4. `pytest_configure` hooks fire.
5. Collection starts.
6. Tests run.
7. `pytest_unconfigure` hooks fire.

Inside step 3, both pytest-django and this package register a
`pytest_load_initial_conftests` hook:

- pytest-django's hook (around `pytest_django/plugin.py:357` in current
  versions): forces `django.setup()`, which imports the user's settings
  module, which reads `DATABASES` from `os.environ` (or from
  django-environ which reads `os.environ`).
- This package's hook: starts containers via #1's makers, writes
  `os.environ`.

Without `tryfirst=True`, pluggy executes hooks LIFO with no order
guarantee between two competing default-priority registrations. In
practice, pytest-django often wins because its plugin is loaded earlier
in the entry-point graph. Result: Django imports settings against stale
`os.environ`, connects to whatever port your `.env` had (typically
`5432`), gets `Connection refused` (or worse: connects to a real
docker-compose Postgres that has stale data), and tests fail with
opaque "OperationalError" messages that look like docker-compose
problems.

### 6.4 The fix: `@pytest.hookimpl(tryfirst=True)`

`@pytest.hookimpl(tryfirst=True)` forces our hook to the front of the
queue. Pluggy honors `tryfirst` deterministically. Even if
pytest-django decides tomorrow to mark its own hook with `tryfirst`
too, `tryfirst` between two implementations falls back to LIFO
registration order, and we control our own registration via entry
points — we can make sure our plugin loads first by virtue of being
the one most directly responsible for env vars.

Counter-question: why not `trylast` on pytest-django? We don't own
pytest-django and patching its decorators is fragile. Our `tryfirst`
is robust.

Counter-question: why not `pytest_configure(tryfirst=True)`? Because
`pytest_configure` fires *after* `pytest_load_initial_conftests`, and
pytest-django uses the latter for `django.setup()`. Hooking
`pytest_configure` is too late.

### 6.5 Numbered sequence (the educational version)

This is the canonical sequence to put in the README and in inline
docstrings. Rehearse it until it's automatic.

1. User runs `pytest`. Shell `os.environ` has e.g. `DJANGO_DB_PORT=5432`
   from direnv / `.env`.
2. pytest discovers our entry point and pytest-django's entry point.
   Plugins are imported (top-level code runs; `register()` calls in the
   user's `conftest.py` execute now).
3. pytest fires `pytest_load_initial_conftests`. Pluggy looks at all
   registered impls.
4. **Our impl, marked `tryfirst=True`, runs first.**
5. We check `--no-testcontainers` and the `disable_env` env var — bail
   if set (user is delegating to docker-compose / pre-existing services).
6. We check `PYTEST_XDIST_WORKER` — if we're a worker, set the
   `skip_dotenv_env` flag and return (the controller already started
   containers and exported env; the worker inherited it on fork/spawn).
   See §7.
7. We resolve config (pyproject.toml + `register()` overrides).
8. We delegate to #1: `make_postgres(image=..., user=..., password=...,
   database=..., env=..., init_scripts=..., port=...)` →
   `PostgresContainer(host="localhost", port=63403, ...)`. If
   `redis_enabled`, `make_redis(...)` similarly.
9. We write to `os.environ`:
   - `<db_host_env>` = `localhost`
   - `<db_port_env>` = `63403`
   - `<db_name_env>` = `<postgres_database>`
   - `<db_user_env>` = `<postgres_user>`
   - `<db_password_env>` = `<postgres_password>`
   - `<db_test_template_env>` = `<postgres_template>` (if configured)
   - `<redis_host_env>` / `<redis_port_env>` (if `redis_enabled`)
   - `<skip_dotenv_env>` = `"1"`
10. We register `atexit.register(_stop)` as a safety net for abrupt-exit
    paths that skip `pytest_unconfigure`.
11. Our hook returns. Pluggy moves on.
12. **pytest-django's impl runs.** It triggers `django.setup()` →
    settings module imports → `DATABASES` is read from the **freshly
    populated** `os.environ`. dotenv loading is skipped because of
    the skip flag, so our injected ports survive.
13. pytest-django sees `DATABASES['default']['TEST']['TEMPLATE'] = "..."`
    (the project's settings code wired the env var to the TEST dict),
    so when it eventually creates the test DB, it does
    `CREATE DATABASE test_<name> WITH TEMPLATE <name>` — instant.
14. Collection runs, fixtures resolve, tests execute.
15. `pytest_unconfigure` fires; we stop containers (unless reuse mode).
16. atexit runs as safety net (no-op if `pytest_unconfigure` already
    cleaned up).

### 6.6 What goes wrong without `tryfirst`

Skip step 4. Suddenly pytest-django runs first at step 12, reads
`DJANGO_DB_PORT=5432`, opens psycopg connection. Either:

- That port has nothing on it → `OperationalError: connection refused`.
- That port has docker-compose Postgres → tests run against the wrong DB,
  pollute developer state, possibly succeed silently (until a test
  expects baseline data and the wrong DB has different rows).

The second case is worse than the first.

### 6.7 What goes wrong without dotenv-skip

Imagine `tryfirst=True` works and our hook ran first, but the project's
`settings.py` does (verbatim from BPP `base.py:186`):

```python
if not os.environ.get("DJANGO_BPP_SKIP_DOTENV"):
    for fn in ENVFILE_PATHS:
        if os.path.exists(fn) and os.path.isfile(fn):
            environ.Env.read_env(fn, overwrite=True)
```

Without the `SKIP_DOTENV` flag set, `read_env(..., overwrite=True)`
clobbers our just-injected `DJANGO_BPP_DB_PORT=63403` with the value
from `.env` (`5432`). Now `os.environ` has `5432` again, settings.py
proceeds to read `DJANGO_BPP_DB_PORT` from `os.environ`, and Django
connects to `5432`. Same failure mode as no-`tryfirst`, different
mechanism. See §9 for the full explanation.

### 6.8 What goes wrong without xdist worker handling

Without the worker check (§7), each xdist worker starts its own
containers — N extra Postgreses fighting for ports, workers connect
to their own (baseline-less) DBs, `CREATE DATABASE … WITH TEMPLATE`
fails. Our `PYTEST_XDIST_WORKER` early-return prevents this.

---

## 7. xdist support

Inherited verbatim from BPP's plugin (`is_xdist_worker()` check in
`plugin.py:54-60`). Logic:

- pytest-xdist's controller process forks/spawns workers. Workers
  inherit the controller's `os.environ`, including the env vars we
  injected.
- In a worker, `PYTEST_XDIST_WORKER` is set to the worker name
  (e.g. `gw0`).
- When we detect a worker, we **do not** start new containers — we'd
  fight with the controller for ports and produce N stale containers.
- We **do** still need to set `<skip_dotenv_env>=1` in the worker.
  Why: the worker re-imports `conftest.py` and then `settings.py`,
  which may trigger django-environ to re-read `.env` and overwrite
  the inherited port. Setting the skip flag short-circuits that.

```python
def _is_xdist_worker() -> bool:
    return "PYTEST_XDIST_WORKER" in os.environ
```

### 7.1 Coexistence with #1 fixtures in workers

Workers START NO CONTAINERS for the eager-started DB+Redis services
we manage. But workers CAN still use #1's fixture-based containers
freely — for example, a session-scoped MinIO via a `tc_minio` fixture
defined in the project's conftest. Each worker gets its own fixture
instance (because pytest-xdist runs `pytest_collection` per worker,
and session-scoped fixtures are per-worker session).

This is fine. Per-worker MinIOs use ephemeral ports and don't conflict.
The eager-start env (DB+Redis) propagates from controller → workers
once at fork; the fixture-managed services are independent per worker.

If a project has a hot per-worker DB requirement (e.g. parallel test
isolation requiring fresh DBs per worker), this package isn't the
right tool — they should drop our eager-start mode and use #1 fixtures
all the way down. We don't try to be clever about per-worker DBs in v1.

### 7.2 Edge case: `--max-worker-restart`

When a worker is replaced mid-run, the new worker still inherits the
controller's env at fork time, so we're fine. `PYTEST_XDIST_WORKER` is
set in the new process, our worker branch executes, no containers
start, env is already present.

---

## 8. Settings overlay (alternative API) — REJECTED for v1

**Considered alternative**: instead of writing `os.environ`, have the
project import `from pytest_testcontainers_django import override_databases`
in `settings.py` and call it after the `DATABASES` dict is constructed.
The override would mutate the dict in place, replacing host/port with
container values. No env-var indirection.

**Why rejected for v1**:

1. **Intrusive**. Asking projects to add a `from pytest_testcontainers_django
   import ...` to `settings.py` is the opposite of zero-touch. The
   import has to be conditional on "are we running under pytest" — but
   `settings.py` doesn't know that without sniffing `sys.argv` or
   stashing a marker, both fragile. The env-var contract is
   declarative: settings reads env vars regardless of who's running.
2. **Django doesn't actually have a clean API to "re-bind DATABASES
   after setup".** You can mutate `settings.DATABASES['default']
   ['HOST']`, but any code that read it during settings import (e.g.
   CONN_MAX_AGE logic, SSL options block, third-party packages reading
   `settings.DATABASES` at import time) keeps the old value. BPP itself
   has such a block: `if (DATABASES["default"]["HOST"] in
   ["localhost", "127.0.0.1"]) or env("DJANGO_BPP_DB_DISABLE_SSL"):`
   that mutates `OPTIONS`. Re-running that logic post-setup is
   project-specific and brittle.
3. **The env-var path is what BPP, and most django-environ-using
   projects, are already doing.** Pre-existing settings already read
   from env. We'd have to teach users a *second* contract.

**v2 maybe**: a `DjangoSettingsOverride` plugin mode for greenfield
projects that don't want django-environ. Out of scope for v1; documented
here so we don't re-litigate.

---

## 9. dotenv handling

The `*_SKIP_DOTENV` env var (BPP: `DJANGO_BPP_SKIP_DOTENV`) is the
**second** load-bearing detail of the package, and it's underdocumented
even in BPP's own codebase.

### 9.1 Why it exists

BPP's `settings/base.py:186`:

```python
if not os.environ.get("DJANGO_BPP_SKIP_DOTENV"):
    for fn in ENVFILE_PATHS:
        if os.path.exists(fn) and os.path.isfile(fn):
            environ.Env.read_env(fn, overwrite=True)
```

Note the `overwrite=True`. django-environ's `read_env(..., overwrite=True)`
will **clobber** anything already in `os.environ` with values from
`.env`. So the sequence
`os.environ["DJANGO_BPP_DB_PORT"] = "63403"` (us) → `read_env(.env, overwrite=True)`
(settings.py at import) leaves us with `DJANGO_BPP_DB_PORT=5432` again.

The skip flag short-circuits this. The settings module reads the flag
**before** dotenv loading, so when the flag is set, dotenv files are
ignored entirely.

### 9.2 Project contract

We document a hard requirement: **the project must respect a configurable
"skip dotenv" env var in its settings file**. We can't enforce this
from inside the plugin (we don't know the user's settings file), but
we'll detect it at first-run by checking whether our injected
`*_DB_PORT` made it through to `connection.settings_dict["PORT"]` and
warn loudly if not.

The flag name is configurable (`skip_dotenv_env`) so projects with no
dotenv loading at all can ignore the contract — they set
`skip_dotenv_env = ""` to disable the injection. This is the
"my settings doesn't use django-environ" escape hatch.

### 9.3 Alternative considered

Auto-monkey-patch django-environ's `Env.read_env`. Rejected:
django-environ isn't a hard dep, projects use python-decouple,
django-configurations, vanilla `os.getenv`, or hand-rolled parsers.
We can't safely patch all of them. The env-var contract is portable.

---

## 10. Init scripts (baseline integration with #3)

### 10.1 Mechanism

Postgres official image (and BPP's `iplweb/bpp_dbserver`) honors
`/docker-entrypoint-initdb.d/`. Any `*.sql`, `*.sql.gz`, or `*.sh` in
that directory runs on first cluster init, **before** the container
starts accepting TCP connections. testcontainers' wait strategy waits
for TCP, so by the time we get a port back, the dump is already loaded.

This is significantly faster than the alternative (start empty container,
`psql -f baseline.sql` from the host) because:

- No host-side `psql` needed.
- No TCP round-trips for every `INSERT`.
- The dump is loaded in a single in-process pass.

### 10.2 Configuration surface

```toml
postgres_init_scripts = [
    "tests/fixtures/baseline.sql",
    "tests/fixtures/extensions.sql",
]
postgres_template = "myapp"
```

Each path is mounted as `/docker-entrypoint-initdb.d/NN-name.sql`
where `NN` is a 2-digit zero-padded index (preserves order). `.sql.gz`
is detected by extension. Script paths in pyproject are interpreted
relative to project root; in `register()` calls, `Path` objects are
resolved as-is (allow absolute paths).

### 10.3 #3 integration

`django-pg-baseline` provides a SQL artifact. Its API contract toward
this package (defined in #3's spec, summarized here):

```python
# django_pg_baseline/api.py — provided by #3
def get_baseline_path() -> Path:
    """Return resolved path to baseline.sql.

    Raises BaselineNotBuiltError if the artifact hasn't been generated.
    Raises BaselineStaleError if the artifact is older than the
    relevant migration set (configurable; #3's policy).
    """
```

Two integration paths:

**Path A — via conftest.py (recommended, primary)**:

```python
# project conftest.py
from django_pg_baseline import get_baseline_path
from pytest_testcontainers_django import register, DjangoContainerConfig, PostgresService

register(DjangoContainerConfig(
    postgres=PostgresService(
        image="postgres:16",
        init_scripts=[get_baseline_path()],
        template="myapp",
        # ... env names
    ),
))
```

Direct Python; no string-based magic. Errors fail fast at import time
with a stack trace pointing at conftest.py.

**Path B — via pyproject.toml flag (supported but secondary)**:

```toml
[tool.pytest-testcontainers-django]
use_django_pg_baseline = true   # imports #3 and prepends its path
postgres_init_scripts = [
    # additional scripts after baseline; baseline is auto-prepended
]
postgres_template = "myapp"
```

When `use_django_pg_baseline = true`, the plugin imports
`django_pg_baseline` and calls `get_baseline_path()` at hook time,
prepending the resolved path to `postgres_init_scripts`. If #3 is
not installed, hook fails with a clear error: install
`django-pg-baseline` or set the flag to `false`.

**Decision**: v1 supports both Path A and Path B. Path A is
documented as the recommended hookup. Path B is for "I want zero
conftest.py" projects.

We **do not** support a string-sigil DSL like `"@django-pg-baseline:..."`
inside `postgres_init_scripts` lists — that's a tiny in-house DSL,
worse than the flag.

### 10.4 TEMPLATE setting wiring

Mounting init scripts is half the story. The other half is telling
Django to clone the loaded DB instead of running migrations from
scratch.

We write `os.environ[db_test_template_env] = postgres_template`. The
project's settings.py is responsible for translating that to
`DATABASES['default']['TEST']['TEMPLATE']`. BPP does this in
`base.py:715-719`:

```python
_test_template = env("DJANGO_BPP_TEST_TEMPLATE")
if _test_template:
    test_settings = DATABASES["default"].get("TEST", {})
    test_settings["TEMPLATE"] = _test_template
    DATABASES["default"]["TEST"] = test_settings
```

Document this snippet as the **canonical settings-side glue**. Copy it
into projects, change the env-var name. Three lines. Don't make this
a Python import users have to do.

### 10.5 Caveat: `postgres_template` and Django's test DB name

Django creates the test DB as `test_<NAME>` by default. Cloning from
the original `<NAME>` works, but means the template DB *is* the
production-shaped DB on the container. This is fine for testcontainers
(the container is throwaway), but document the gotcha for users
running this against a shared/persistent DB (which they shouldn't,
but).

### 10.6 Default: `postgres_template` follows `postgres_database`

When `postgres_init_scripts` is non-empty and `postgres_template`
is unset, the plugin defaults `postgres_template = postgres_database`.
Reasoning: init scripts replay into `postgres_database` (that's
what the Postgres entrypoint does), so the only DB that has the
loaded baseline is `postgres_database`. Cloning a different name
would clone an empty `template0`/`template1`. This DRY default
prevents the most common "I set up the dump but tests still see
empty tables" footgun. User can override explicitly for advanced
setups (see open question §14.9).

### 10.7 Reuse mode + init scripts

Postgres runs `/docker-entrypoint-initdb.d/` **only on first cluster
init**. When reuse mode attaches to a pre-existing container:

```
[pytest-testcontainers-django] reuse mode + init_scripts:
init scripts NOT replayed against the existing container
(Postgres only runs /docker-entrypoint-initdb.d/ on first init).
To re-apply: stop and remove the container, then re-run.
Suggested: docker rm -f <container-name>
```

This is a one-line stderr warning, **not a failure** — tests run
against whatever state the existing container has. v1 does not
hash-fingerprint init scripts. Reasoning: reuse mode is for fast
iteration on a known-good baseline; baselines change rarely, and
when they do you bump version → fresh container → init scripts
replay. (See open question §14.10 for hash-check follow-up.)

---

## 11. Redis support

BPP's plugin starts Redis alongside Postgres. Redis is in scope here
for ergonomics — most Django projects with a DB need a cache, and the
timing dance is identical (settings read `REDIS_HOST` / `REDIS_PORT`
at import time; same race, same fix).

### 11.1 Decision: Redis is first-class, but other containers go through #1 fixtures

`redis_enabled=true` toggles a built-in Redis service. We do not
generalize to "configure N arbitrary containers from pyproject.toml".
Reasons:

- Two well-known services (Postgres, Redis) cover ~95% of Django stacks.
- Generalizing to "any container" reinvents docker-compose poorly.
- For the 5% case (Elasticsearch, Kafka, Localstack, MinIO), the user
  declares a fixture using #1's maker functions directly. That's late
  enough, because non-DB services typically read their host:port from
  settings at *connection time* rather than import time. The race only
  matters for `DATABASES`. See §3.3 for the coexistence pattern.

### 11.2 Surface

See §4.1. Redis env names map to `redis_host_env` and `redis_port_env`
only — DB number, password, sentinel, etc., are out of scope. Project
settings can fold the host:port into a `redis://...` URL.

### 11.3 BPP-specific Redis env

BPP also sets `DJANGO_BPP_REDIS_DB_BROKER`, `DJANGO_BPP_REDIS_DB_CACHE`,
etc. — those are **logical DB indices**, static, not container-related.
We do not touch those. Project settings retain them exactly as today.

---

## 12. Repo layout, CI, deps

Standard `src/pytest_testcontainers_django/` layout with modules for:
plugin (hook impls), config (pyproject parsing, validation), injection
(os.environ writer), containers (bridge to #1's makers), xdist (worker
detection), and `_types`. Tests cover hook ordering (our hook before
pytest-django), env injection, dotenv skip, xdist workers, init
scripts, template DB, and #3 integration.

**Runtime deps**: `pytest>=7,<9`, `pytest-django>=4`,
`pytest-testcontainers` (#1, version-pinned to the maker function API
in §3), `tomli; python_version<'3.11'`.

**Optional deps**: `pytest-xdist` (detected via env var, no install-time
dep), `django-pg-baseline` (#3, activated by
`use_django_pg_baseline = true`).

**Support matrix**: Python 3.10+, Django 4.2 LTS / 5.0 / 5.1 / 5.2 LTS,
pytest-django 4.x. CI matrix on GitHub Actions with a Docker service
container providing the daemon.

**Entry point**:

```toml
[project.entry-points."pytest11"]
testcontainers_django = "pytest_testcontainers_django.plugin"
```

Auto-loaded — no `-p` flag or `conftest.py` import required.

**PyPI name**: `pytest-testcontainers-django` is available (404 on
`pypi.org/pypi/pytest-testcontainers-django/json/`).

---

## 13. Generalization checklist (BPP → generic)

Mapping of every BPP-specific identifier currently embedded in
`src/testcontainers_bpp/plugin.py` and `containers.py` to its
generalized form. After the architectural split, **all** of these
live in #2's config (either `pyproject.toml` or `register()` args) —
none of them leaks into #1.

| BPP today                                  | Generalized config key      | Default                  |
| ------------------------------------------ | --------------------------- | ------------------------ |
| `DJANGO_BPP_DB_HOST` (env name)            | `db_host_env`               | `DJANGO_DB_HOST`         |
| `DJANGO_BPP_DB_PORT`                       | `db_port_env`               | `DJANGO_DB_PORT`         |
| `DJANGO_BPP_DB_NAME`                       | `db_name_env`               | `DJANGO_DB_NAME`         |
| `DJANGO_BPP_DB_USER`                       | `db_user_env`               | `DJANGO_DB_USER`         |
| `DJANGO_BPP_DB_PASSWORD`                   | `db_password_env`           | `DJANGO_DB_PASSWORD`     |
| `DJANGO_BPP_TEST_TEMPLATE`                 | `db_test_template_env`      | `DJANGO_DB_TEST_TEMPLATE`|
| `DJANGO_BPP_SKIP_DOTENV`                   | `skip_dotenv_env`           | `DJANGO_SKIP_DOTENV`     |
| `DJANGO_BPP_REDIS_HOST`                    | `redis_host_env`            | `DJANGO_REDIS_HOST`      |
| `DJANGO_BPP_REDIS_PORT`                    | `redis_port_env`            | `DJANGO_REDIS_PORT`      |
| `iplweb/bpp_dbserver:psql-16.13`           | `postgres_image`            | `postgres:16`            |
| `bpp` / `password` / `bpp` (pg credentials)| `postgres_user` / `postgres_password` / `postgres_database` | `postgres` ×3 |
| `5432`                                     | `postgres_internal_port`    | `5432`                   |
| `redis:7-alpine`                           | `redis_image`               | `redis:7-alpine`         |
| `6379`                                     | `redis_internal_port`       | `6379`                   |
| `bpp` (template DB name)                   | `postgres_template`         | unset                    |
| `BPP_USE_TESTCONTAINERS`                   | `disable_env`               | `PYTEST_TESTCONTAINERS_DISABLE` |
| `BPP_TESTCONTAINERS_REUSE`                 | `reuse_env`                 | `PYTEST_TESTCONTAINERS_REUSE`   |
| `BPP_BASELINE_SQL_PATH`                    | (removed)                   | use `postgres_init_scripts` directly  |
| `bpp-tc-pg` / `bpp-tc-redis` (reuse names) | (delegated to #1's reuse)   | #1 picks names           |
| `POSTGRESQL_UNSAFE_BUT_FAST=1`             | `postgres_env` (mapping)    | empty                    |
| `POSTGRESQL_MAX_LOCKS_PER_TRANSACTION=512` | `postgres_env` (mapping)    | empty                    |
| docker-compose label override hack         | (delegated to #1)           | #1 handles it            |
| `find_baseline_sql()` convention path      | (removed; use explicit path or #3) | —                 |
| `--no-testcontainers` CLI flag             | retained, same name         | —                        |

Items removed entirely:

- Convention-based `baseline.sql` discovery (`src/baseline-sql/baseline.sql`).
  Replaced by explicit `postgres_init_scripts` paths or via #3's
  `get_baseline_path()`.
- Hard-coded `bpp-tc-pg` / `bpp-tc-redis` container names. Container
  naming is an implementation detail of #1's reuse mode (see §14
  open question 11 for how reuse-mode worker names should compose).
- Docker-compose label scrubbing (`com.docker.compose.*`). That's a
  defensive workaround for users who built `iplweb/bpp_dbserver` via
  `docker compose build` historically. Out of scope for a generic
  plugin; if someone reproduces the bug, fix it in #1 or in their
  build flow.

---

## 14. Open questions

Some questions from earlier drafts (about timing — "could fixtures
work?", "could `pytest_configure` work?") are now resolved in §6 and
no longer listed.

1. **Hook collision with future pytest-django versions.** If pytest-django
   ever marks its `pytest_load_initial_conftests` hook with `tryfirst=True`
   too, both hooks become equally privileged and pluggy falls back to
   registration order. We should add a regression test that imports
   pytest-django and asserts our hook runs first by inspecting the
   resolved hook chain. Question: is there a public pluggy API for
   that, or do we have to rely on the side effect (env var written
   before settings imported)?

2. **Multi-database projects.** Django supports multiple `DATABASES`
   entries (`default`, `replica`, `analytics`). v1 supports one. Is
   there demand for multi-DB? (Probably yes long-term; defer to v2,
   the config model already extends naturally with a list of
   `PostgresService`s.)

3. **Settings autodetection.** Could we sniff the user's settings file
   to detect their env-var names automatically? Tempting, fragile.
   Probably no — we'd parse Python AST, miss django-environ's
   `env(...)` indirection half the time. Lean toward explicit config.

4. **`DATABASE_URL` users.** §5.3 punted. If demand is loud, add a
   `database_url_env` setting that composes `postgres://USER:PASS@HOST:PORT/DB`
   into a single env var. Easy to add, just deferred. (`psycopg` 3 /
   async is orthogonal — we just hand back `host:port`.)

5. **Coexistence with `pytest-postgresql`.** Overlapping scope (it
   also provides a Postgres). Document: "do not enable both"; add an
   early-fail check that warns if `pytest_postgresql` is loaded
   alongside us.

6. **Settings without django-environ.** Settings read directly from
   `os.environ`. Then `skip_dotenv_env` is moot — user sets
   `skip_dotenv_env = ""` to disable that injection. Worth a docs
   example.

7. **CI service-container detection.** GitHub Actions / GitLab CI
   often provide Postgres as a service. Current answer: explicit
   `disable_env=1` in CI; no autodetection magic.

8. **Reuse-mode container names per worker.** #1's reuse mode uses a
   `<project>-tc-<service>` template. For #2's eager-start path,
   we run a single shared container per service on the controller
   (workers inherit env). If a future use case needs per-worker
   eager-started containers, propose `<project>-tc-<service>-<worker_id>`
   — defer impl until needed.

9. **`postgres_template` vs `postgres_database` semantics.**
   **RESOLVED (user, 2026-05-08): same value is the default; allow
   them to differ for advanced setups; document the gotcha.**

   - `postgres_database` — name of the DB the container creates at
     startup (passed as `POSTGRES_DB`). This is also the DB into
     which the official Postgres entrypoint replays
     `/docker-entrypoint-initdb.d/` (i.e., where `baseline.sql`
     lands). Default `"postgres"`.
   - `postgres_template` — value injected as
     `DATABASES['default']['TEST']['TEMPLATE']` in Django settings,
     causing pytest-django to issue
     `CREATE DATABASE test_<X> WITH TEMPLATE <postgres_template>`.

   In the **typical case they MUST be equal** — the template needs
   to contain the seed data, which means it must be the DB where
   init scripts ran, which means `postgres_template = postgres_database`.

   The advanced case (different values) is when init scripts
   themselves create a separate seed DB:
   ```sql
   CREATE DATABASE seed_template TEMPLATE template0;
   \c seed_template
   -- load fixtures into seed_template
   ```
   Then `postgres_database = "myapp"` (default app DB, can be empty)
   and `postgres_template = "seed_template"`.

   **Default behavior**: if `postgres_template` is unset and
   `postgres_init_scripts` is non-empty, default
   `postgres_template = postgres_database` (DRY). User can override.

   **Collision concern (template DB locked when a connection is
   open) is not a real issue in pytest-django flow**: setup_databases
   creates `test_<X>` BEFORE any connection opens to the source DB.
   Django connects to `test_<X>` from there on, never to the
   template. Document the gotcha for users running this against
   a shared/persistent DB (which they shouldn't, but).

10. **Init-script idempotency on container reuse.** Init scripts in
    `/docker-entrypoint-initdb.d/` only run on **first** Postgres
    init. If the user changes `postgres_init_scripts` and reuses the
    existing container, the new scripts won't run. Symptom: "I added
    a fixture SQL but tests don't see it."

    **RESOLVED (user, 2026-05-08): warn-and-continue.** When reuse
    mode is active AND `postgres_init_scripts` is non-empty AND we
    detect we're attaching to a pre-existing container (not creating
    fresh), emit one stderr warning at startup:

    ```
    [pytest-testcontainers-django] reuse mode + init_scripts:
    init scripts NOT replayed against the existing container
    (Postgres only runs /docker-entrypoint-initdb.d/ on first init).
    To re-apply: stop and remove the container, then re-run.
    Suggested: docker rm -f <container-name>
    ```

    Continue normally — tests run against whatever state the existing
    container has. No hash fingerprinting in v1; hash-check stays
    deferred as a follow-up if users hit this often. The reasoning
    is that reuse mode is for fast iteration on a known-good baseline;
    if you change the baseline you also bump version, which means
    fresh container, which means scripts run normally.

---

## 15. Boundaries with #1 and #3

What this package **does not** do (and where the responsibility lives).
After the architectural split, the boundaries are sharp.

### 15.1 #1 (`pytest-testcontainers`) owns

- Maker functions: `make_postgres`, `make_redis`, `make_minio`, etc.
- Docker daemon ping, error message (`DockerNotRunningError`).
- Image pull, container start, port resolution, readiness wait.
- Named-container reuse logic (when invoked through reuse-aware
  fixtures or maker arguments).
- Ephemeral cleanup, Ryuk integration.
- Hybrid fixture patterns for users who want session/module/function
  scoped containers in non-Django contexts.
- Used standalone for non-Django projects (FastAPI, Flask, plain
  library testing).
- Used **alongside** #2 for extra services beyond DB+Redis (Elasticsearch,
  MinIO, Kafka) in Django projects.

### 15.2 #2 (this package) owns

- `pytest_load_initial_conftests(tryfirst=True)` hook.
- Reading project config from `[tool.pytest-testcontainers-django]`
  pyproject table.
- `register()` API for programmatic config.
- `os.environ` injection: DB host/port/name/user/password, Redis
  host/port, TEST TEMPLATE, SKIP_DOTENV.
- Init-scripts mount contract (path → `/docker-entrypoint-initdb.d/NN-name.sql`).
- `DATABASES['TEST']['TEMPLATE']` semantics (env var that settings.py
  is documented to consume).
- xdist worker detection and skip-start behavior.
- pytest-django plugin coexistence (registration order, cleanup
  ordering on teardown).
- `--no-testcontainers` CLI flag and `disable_env` env var.
- atexit safety net.
- Eager-start lifecycle on the controller: start in
  `pytest_load_initial_conftests`, stop in `pytest_unconfigure` +
  atexit.

### 15.3 #3 (`django-pg-baseline`) owns

- Building / refreshing `baseline.sql`.
- Versioning and validating baseline schema.
- `pg_dump`-based baseline export.
- `pg_restore` / `psql` post-CREATE-DATABASE (its monkey-patch mode,
  if used).
- Resolving baseline path via `get_baseline_path() -> Path`.
- Freshness checks (artifact older than migration set → error).

### 15.4 Two-way contracts

- **#1 → #2**: stable maker function signatures (`make_postgres`,
  `make_redis` returning handles with `.host`, `.port`, `.stop()`)
  and `DockerNotRunningError`. See §3.
- **#3 → #2**: `get_baseline_path() -> Path` helper. #2 calls it from
  conftest (Path A) or via `use_django_pg_baseline = true` flag (Path B).
- **#2 → user project**: settings.py reads configured env vars at
  import time and respects the skip-dotenv flag. Canonical TEST
  TEMPLATE snippet (§10.4) in README.

Breaking any of these contracts → SemVer-major release on the
affected packages, coordinated.

### 15.5 Summary table

| Concern                                    | Owner |
| ------------------------------------------ | ----- |
| Docker daemon ping, maker functions, reuse, Ryuk, fixture patterns | #1 |
| `pytest_load_initial_conftests(tryfirst=True)` hook & ordering | #2 |
| `os.environ` injection (DB, Redis, TEST TEMPLATE, SKIP_DOTENV)   | #2 |
| Init-scripts mount contract, `--no-testcontainers`, xdist worker propagation | #2 |
| Building/refreshing/versioning `baseline.sql`, `get_baseline_path()` | #3 |
