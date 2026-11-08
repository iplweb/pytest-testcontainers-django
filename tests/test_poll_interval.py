"""``poll_interval_override``: faster readiness polling, same startup timeout."""

from __future__ import annotations

import pytest
from testcontainers.core.config import testcontainers_config as tc
from testcontainers.core.waiting_utils import WaitStrategy

from pytest_testcontainers_django.containers import poll_interval_override


class _Probe(WaitStrategy):
    def wait_until_ready(self, container):  # pragma: no cover - never called
        raise NotImplementedError


@pytest.fixture
def _tc_defaults():
    old = tc.sleep_time, tc.max_tries
    tc.sleep_time, tc.max_tries = 1.0, 120
    yield
    tc.sleep_time, tc.max_tries = old


@pytest.mark.usefixtures("_tc_defaults")
def test_override_sets_interval_and_keeps_timeout() -> None:
    with poll_interval_override(0.1):
        assert tc.sleep_time == 0.1
        assert tc.timeout == pytest.approx(120.0)
        # Wait strategies built inside the block pick the values up.
        probe = _Probe()
        assert probe._poll_interval == 0.1
        assert probe._startup_timeout == pytest.approx(120.0)
    assert (tc.sleep_time, tc.max_tries) == (1.0, 120)


@pytest.mark.usefixtures("_tc_defaults")
def test_override_restores_on_error() -> None:
    with pytest.raises(RuntimeError), poll_interval_override(0.25):
        raise RuntimeError("boom")
    assert (tc.sleep_time, tc.max_tries) == (1.0, 120)


@pytest.mark.usefixtures("_tc_defaults")
def test_override_none_is_noop() -> None:
    with poll_interval_override(None):
        assert (tc.sleep_time, tc.max_tries) == (1.0, 120)
