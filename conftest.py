"""Project-root conftest — disable the plugin's own eager-start during
the tests of *this* package.

Why here and not in ``tests/conftest.py``: the plugin's
``pytest_load_initial_conftests(tryfirst=True)`` hook preloads the
**rootdir** ``conftest.py`` (so users can call ``register()`` from
there).  ``tests/conftest.py`` is only loaded later by pytest's
default-priority hook — too late to flip the disable flag before the
plugin reads it.  Putting the flag in the root conftest guarantees it's
in ``os.environ`` by the time ``is_disabled()`` runs.

``setdefault`` so CI / individual invocations can still flip the var
explicitly if they want the live-Docker path (none of our tests do).
"""

from __future__ import annotations

import os

os.environ.setdefault("PYTEST_TESTCONTAINERS_DISABLE", "1")
