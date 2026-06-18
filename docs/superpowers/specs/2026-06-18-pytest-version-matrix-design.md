# Design: pytest 7/8/9 test matrix (local + CI)

Date: 2026-06-18
Status: Approved

## Problem

The package's core value is winning a pytest hook-ordering race against
`pytest-django` (`pytest_load_initial_conftests(tryfirst=True)`, the
`tryfirst`/`trylast` loader, `pytest11` entry-point auto-loading). These are
exactly the APIs a *major* pytest release can change. Today:

- `pyproject.toml` caps the runtime dependency at `pytest >=7.4,<9`, so pytest 9
  cannot even install. The cap is precautionary, not a known incompatibility.
- CI (`.github/workflows/ci.yml`) matrixes only over **Python** (3.10–3.13). The
  pytest version is whatever `uv` resolves — currently 8.x.
- There is **no local multi-version runner** (no `tox`/`nox`).

We want to test pytest 7.x, 8.x, and 9.x — both locally and on CI — and stop
blocking pytest-9 installs for users once the matrix is green.

## Confirmed compatibility facts (PyPI, 2026-06-18)

- Latest per major: pytest **7.4.4**, **8.4.2**, **9.1.0**.
- `requires_python`: pytest 7.4.4 `>=3.7`, 8.4.0 `>=3.9`, **9.0.0 `>=3.10`**.
  The pytest-9 floor (3.10) aligns exactly with this project's `requires-python
  = ">=3.10"`, so the full Python axis is valid for pytest 9.
- pytest gained official **Python 3.13** support in **8.3**. pytest 7.4.x on
  Python 3.13 is unsupported → the single dead cell.
- `pytest-django 4.12.0` pins only `pytest>=7.0.0` (no upper cap) — it does not
  block pytest 9. Whether it *functionally* works on pytest 9 is what the matrix
  verifies.

### Matrix grid (Python × pytest major)

|         | pytest 7 (7.4.4) | pytest 8 (8.4.2) | pytest 9 (9.1.0) |
| ------- | ---------------- | ---------------- | ---------------- |
| py3.10  | ✓                | ✓                | ✓                |
| py3.11  | ✓                | ✓                | ✓                |
| py3.12  | ✓                | ✓                | ✓                |
| py3.13  | ✗ (pre-8.3)      | ✓                | ✓                |

One excluded cell (`py3.13 × pytest7`) → **11 jobs**.

## Decisions

1. **Runtime cap** — lift `pytest >=7.4,<9` to `pytest >=7.4,<10` in
   `pyproject.toml` `dependencies`. Declares pytest-9 support; the matrix gates
   it before any release tag.
2. **Local runner** — `tox` + `tox-uv`. Single source of truth for the matrix;
   uv-backed envs for speed. Add `tox-uv` to the `dev` extra.
3. **Matrix shape** — full cross-product Python 3.10–3.13 × pytest {7,8,9} with
   the `py3.13 × pytest7` cell excluded, in both `tox.ini` and CI.
4. **CI delegates to tox** — CI invokes `tox -e <env>` so CI and local exercise
   byte-identical envs. No bespoke CI install script for the test cells.
5. **Lint split out** — move `ruff check`/`ruff format --check` into its own CI
   job that runs once, instead of redundantly inside every matrix cell.

## Components

### `pyproject.toml`

- `dependencies`: `pytest >=7.4,<9` → `pytest >=7.4,<10`.
- `optional-dependencies.dev`: add `tox` and `tox-uv` (so `tox` is on PATH after
  `uv pip install -e '.[dev]'`).
- Update the inline rationale near the pin (currently none) to a short comment
  pointing at this matrix as the gate for the upper bound.

### `tox.ini` (new)

```ini
[tox]
envlist =
    py{310,311,312,313}-pytest{8,9}
    py{310,311,312}-pytest7
requires =
    tox-uv>=1

[testenv]
runner = uv-venv-lock-runner
extras = dev
deps =
    pytest7: pytest>=7.4,<8
    pytest8: pytest>=8,<9
    pytest9: pytest>=9,<10
setenv =
    PYTEST_TESTCONTAINERS_DISABLE = 1
    PYTEST_TESTCONTAINERS = 0
commands =
    pytest -v
```

- The factor-conditional `deps` pin the pytest major; it wins over the `.[dev]`
  resolution because it is an explicit constraint compatible with the relaxed
  `<10` cap.
- `setenv` mirrors today's CI unit-test run (containers disabled).
- The envlist encodes the exclude structurally — `pytest7` is only paired with
  py310/311/312.

Usage: `tox` (all cells), `tox -e py312-pytest9` (one cell).

### `.github/workflows/ci.yml`

- **New `lint` job**: checkout → setup-python (one version) → install uv →
  `uv pip install -e '.[dev]'` → `ruff check src tests` +
  `ruff format --check src tests`. Runs once.
- **`test` job**: add a second matrix axis `pytest-version: ["7","8","9"]` with
  `exclude: { python-version: "3.13", pytest-version: "7" }`. Steps: checkout →
  setup-python `${{ matrix.python-version }}` → install uv → install tox+tox-uv
  → run `tox -e py<XY>-pytest<P>`, where `<XY>` is the Python version with the
  dot removed and `<P>` is the pytest axis value. tox uses the matrix Python as
  the env base interpreter, so the GitHub matrix and tox envlist stay aligned.
  A comment in `ci.yml` notes that the exclude is duplicated in `tox.ini`'s
  envlist and why.

## Data flow

```
CI matrix cell (python=3.12, pytest=9)
  └─ setup-python 3.12  ──►  tox -e py312-pytest9
                               └─ tox-uv creates uv venv on python3.12
                                    └─ installs .[dev] + pytest>=9,<10
                                         └─ pytest -v  (containers disabled via setenv)
```

Local `tox` runs the same envs against locally available interpreters.

## Error handling / risks

- **`filterwarnings = ["error"]`** (`pyproject.toml`): a *new* pytest-9
  DeprecationWarning becomes a hard test failure. This is intended — the matrix
  is the canary. If pytest 9 surfaces a benign new warning from a dependency, we
  add a scoped `ignore::DeprecationWarning:<module>.*` entry (matching the
  existing docker/testcontainers ignores), never a blanket ignore.
- **`pytest-xdist`** (dev extra) must resolve against pytest 9; if it caps below
  9, bump it in the `dev` extra. The matrix will reveal this at install time.
- **tox factor pin vs `.[dev]`**: if uv ever prefers the extra's resolution over
  the explicit `deps` pin, pin via `deps` ordering / `--reinstall`; verify the
  installed pytest version is logged in CI (`pytest --version` is implicit in
  `-v` header).
- **Missing local interpreters**: `tox` skips/errors envs whose base Python is
  absent locally; that is acceptable for local dev (CI provides each Python).

## Testing / acceptance

1. `tox -e py312-pytest7`, `-pytest8`, `-pytest9` each pass locally (Python 3.12
   present), and the pytest header shows the expected major.
2. `tox` runs the full local matrix (cells with a missing interpreter skipped).
3. CI: 11 test cells + 1 lint job; `py3.13 × pytest7` absent; all green.
4. `uv pip install -e .` still resolves pytest (now up to 9.x) with no conflict.

## Scope / non-goals

- Container **integration** tests are out of this matrix (disabled here, same as
  today's unit run). A separate `integration` tox env is a possible follow-up.
- Python axis stays 3.10–3.13 (pytest-9 floor is 3.10 — already aligned).
- No change to plugin source code is anticipated; if a pytest-9 hook-API change
  breaks a test, that fix is tracked as a separate change, not this spec.

## Implementation outcome (2026-06-18)

Running the matrix locally before writing CI surfaced two things the original
plan did not anticipate:

1. **pytest 7 was broken at the architecture level, not just the cap.** The
   plugin's preload calls the private `pluginmanager._loadconftestmodules(...)`,
   which **does not exist in pytest 7** (it is `_getconftestmodules(path,
   importmode, rootpath)` there, and there is no `consider_namespace_packages`
   ini). On pytest 7 the call raised, was swallowed by the broad `except`, the
   rootdir conftest was never preloaded, and 6 tests failed. The package had
   therefore never actually worked on its advertised `pytest>=7.4` floor.
   **Decision: keep the 7.4 floor and add a feature-detection shim** in
   `plugin.py` — dispatch on `getattr(pm, "_loadconftestmodules", None)`; fall
   back to `_getconftestmodules(path, importmode, rootpath)` on pytest 7. The
   `consider_namespace_packages` getini now lives inside the pytest-8+ branch
   (the ini only exists there), so no separate guard is needed.

2. **pytest 9 was hard-blocked upstream, not by our cap.** `pytest-testcontainers
   0.1.0` (then the only release) pinned `pytest<9`, so lifting our own cap was
   necessary but not sufficient. This was resolved by the upstream release of
   `pytest-testcontainers 0.2.0` (`pytest<10,>=7.4`); with it, pytest 9 installs
   and the suite passes. Our dependency `pytest-testcontainers >=0.1,<2` is
   unchanged — the resolver selects 0.2.0 automatically whenever pytest 9 is
   requested.

Verification: full local `tox` matrix (11 cells) green — pytest 7.4.4 / 8.4.2 /
9.1.0 all 41 passed across Python 3.10-3.13, `py313-pytest7` excluded. `ruff
check` + `ruff format --check` clean. No pytest 10 exists yet (latest 9.1.0);
the `<10` cap simply holds a future major out until it is validated the same way.
