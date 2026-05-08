"""Shared test setup.

Disable the plugin's own eager-start during the tests of the plugin —
otherwise importing pytest in the test runner would try to spin up a
Postgres container before unit-test collection.
"""

from __future__ import annotations

import os

os.environ.setdefault("PYTEST_TESTCONTAINERS_DISABLE", "1")
