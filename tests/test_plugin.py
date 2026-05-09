"""End-to-end plugin tests (SPEC §6, §7).

Uses ``pytester`` (no live Docker) by patching the containers module's
:func:`start_postgres` and :func:`start_redis` to fakes that just hand
back a synthetic ``ContainerHandle``.  This covers the hook ordering,
env injection, xdist worker branch, and ``--no-testcontainers`` flag
without needing the daemon.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

CONFTEST_PATCH = """
from unittest.mock import MagicMock
from pytest_testcontainers_django import containers as _containers
from pytest_testcontainers_django.containers import ContainerHandle


def _fake_pg(**kwargs):
    return ContainerHandle(
        host="127.0.0.1",
        port=63403,
        name=kwargs.get("reuse_name"),
        is_bound_to_existing=False,
        _instance=MagicMock(),
        _owns_instance=True,
    )


def _fake_redis(**kwargs):
    return ContainerHandle(
        host="127.0.0.1",
        port=63479,
        name=kwargs.get("reuse_name"),
        is_bound_to_existing=False,
        _instance=MagicMock(),
        _owns_instance=True,
    )


_containers.start_postgres = _fake_pg
_containers.start_redis = _fake_redis

# Re-bind the references inside the plugin module too — the plugin
# imported them by name at module load time.
from pytest_testcontainers_django import plugin as _plugin
_plugin.start_postgres = _fake_pg
_plugin.start_redis = _fake_redis
"""


def _bootstrap(pytester: pytest.Pytester, *, pyproject_extra: str = "", conftest_extra: str = ""):
    pytester.makepyprojecttoml(
        f"""
[tool.pytest-testcontainers-django]
{pyproject_extra}
"""
    )
    pytester.makeconftest(CONFTEST_PATCH + "\n" + conftest_extra)


@pytest.fixture(autouse=True)
def _isolate_plugin_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset env + module-level state between pytester invocations.

    Pytester runs sub-pytest sessions in the same parent process; without
    this, ``_REGISTERED`` and other globals from one scenario contaminate
    the next.
    """
    from pytest_testcontainers_django import config as _cfg
    from pytest_testcontainers_django import plugin as _plg

    for key in [
        "DJANGO_DB_HOST",
        "DJANGO_DB_PORT",
        "DJANGO_DB_NAME",
        "DJANGO_DB_USER",
        "DJANGO_DB_PASSWORD",
        "DJANGO_DB_TEST_TEMPLATE",
        "DJANGO_SKIP_DOTENV",
        "DJANGO_REDIS_HOST",
        "DJANGO_REDIS_PORT",
        "PYTEST_TESTCONTAINERS_DISABLE",
    ]:
        monkeypatch.delenv(key, raising=False)

    _cfg._reset_registered()
    _plg._reset_state()
    yield
    _cfg._reset_registered()
    _plg._reset_state()


def test_hook_injects_env_vars_with_defaults(pytester: pytest.Pytester) -> None:
    _bootstrap(pytester)
    pytester.makepyfile(
        """
        import os

        def test_db_env_set():
            assert os.environ["DJANGO_DB_HOST"] == "127.0.0.1"
            assert os.environ["DJANGO_DB_PORT"] == "63403"
            assert os.environ["DJANGO_DB_NAME"] == "postgres"
            assert os.environ["DJANGO_DB_USER"] == "postgres"
            assert os.environ["DJANGO_DB_PASSWORD"] == "postgres"
            assert os.environ["DJANGO_SKIP_DOTENV"] == "1"
        """
    )
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_hook_writes_redis_env_when_enabled(pytester: pytest.Pytester) -> None:
    _bootstrap(
        pytester,
        pyproject_extra="""
redis_enabled = true
""",
    )
    pytester.makepyfile(
        """
        import os

        def test_redis_env_set():
            assert os.environ["DJANGO_REDIS_HOST"] == "127.0.0.1"
            assert os.environ["DJANGO_REDIS_PORT"] == "63479"
        """
    )
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_no_testcontainers_flag_skips_injection(pytester: pytest.Pytester) -> None:
    _bootstrap(pytester)
    pytester.makepyfile(
        """
        import os

        def test_db_env_unset():
            assert "DJANGO_DB_HOST" not in os.environ
            assert "DJANGO_SKIP_DOTENV" not in os.environ
        """
    )
    result = pytester.runpytest("-p", "no:cacheprovider", "--no-testcontainers")
    result.assert_outcomes(passed=1)


def test_disable_env_var_skips_injection(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap(pytester)
    pytester.makepyfile(
        """
        import os

        def test_db_env_unset():
            assert "DJANGO_DB_HOST" not in os.environ
        """
    )
    monkeypatch.setenv("PYTEST_TESTCONTAINERS_DISABLE", "1")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_xdist_worker_branch_only_sets_skip_dotenv(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap(pytester)
    pytester.makepyfile(
        """
        import os

        def test_worker_branch():
            assert os.environ["DJANGO_SKIP_DOTENV"] == "1"
            assert "DJANGO_DB_HOST" not in os.environ
        """
    )
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_register_overrides_pyproject(pytester: pytest.Pytester) -> None:
    _bootstrap(
        pytester,
        pyproject_extra="""
postgres_database = "from_pyproject"
""",
        conftest_extra="""
from pytest_testcontainers_django import register, DjangoContainerConfig, PostgresService
register(DjangoContainerConfig(postgres=PostgresService(database="from_register")))
""",
    )
    pytester.makepyfile(
        """
        import os

        def test_register_wins():
            assert os.environ["DJANGO_DB_NAME"] == "from_register"
        """
    )
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_init_scripts_default_template_to_database(
    pytester: pytest.Pytester, tmp_path: Path
) -> None:
    sql = pytester.path / "baseline.sql"
    sql.write_text("-- empty", encoding="utf-8")
    _bootstrap(
        pytester,
        pyproject_extra=f"""
postgres_database = "myapp"
postgres_init_scripts = ["{sql.name}"]
""",
    )
    pytester.makepyfile(
        """
        import os

        def test_template_env_set():
            assert os.environ["DJANGO_DB_TEST_TEMPLATE"] == "myapp"
        """
    )
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_use_django_pg_baseline_triggers_template_default(
    pytester: pytest.Pytester,
) -> None:
    """B2 regression: when only the auto-prepended baseline is in init_scripts,
    apply_template_default still has to see it to default the template.
    """
    sql = pytester.path / "baseline.sql"
    sql.write_text("-- baseline", encoding="utf-8")
    _bootstrap(
        pytester,
        pyproject_extra="""
postgres_database = "myapp"
use_django_pg_baseline = true
""",
        conftest_extra=f"""
import pathlib, sys, types
fake = types.ModuleType("django_pg_baseline")
fake.get_baseline_path = lambda: pathlib.Path({str(sql)!r})
sys.modules["django_pg_baseline"] = fake
""",
    )
    pytester.makepyfile(
        """
        import os

        def test_template_defaulted_from_baseline_only():
            assert os.environ["DJANGO_DB_TEST_TEMPLATE"] == "myapp"
        """
    )
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_use_django_pg_baseline_missing_dep_raises(pytester: pytest.Pytester) -> None:
    """N4 regression: clear error when flag is set but dep isn't installed."""
    _bootstrap(
        pytester,
        pyproject_extra="""
use_django_pg_baseline = true
""",
        conftest_extra="""
import sys
# Ensure the import fails inside the hook even if the package is installed
# at the outer level.
sys.modules["django_pg_baseline"] = None
""",
    )
    pytester.makepyfile("def test_dummy(): pass")
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.stderr.fnmatch_lines(["*django-pg-baseline*"])
    assert result.ret != 0


def test_baseline_path_resolution_does_not_mutate_register_config(
    pytester: pytest.Pytester,
) -> None:
    """B1 regression: re-invoking the hook (or sharing one PostgresService
    instance across processes via register()) must not keep prepending
    the baseline path to init_scripts.
    """
    sql = pytester.path / "baseline.sql"
    sql.write_text("-- baseline", encoding="utf-8")
    _bootstrap(
        pytester,
        conftest_extra=f"""
import pathlib, sys, types
fake = types.ModuleType("django_pg_baseline")
fake.get_baseline_path = lambda: pathlib.Path({str(sql)!r})
sys.modules["django_pg_baseline"] = fake

from pytest_testcontainers_django import (
    DjangoContainerConfig, PostgresService, register,
)

# A user might construct one shared config and register it; the hook must
# not mutate it.
SHARED_PG = PostgresService(database="myapp")
register(DjangoContainerConfig(postgres=SHARED_PG, use_django_pg_baseline=True))
""",
    )
    pytester.makepyfile(
        """
        def test_init_scripts_not_doubled():
            from pytest_testcontainers_django import plugin as p
            from pytest_testcontainers_django import config as c
            # The user's PostgresService.init_scripts must still be empty —
            # the hook should have built a NEW config, not pushed onto theirs.
            registered = c._REGISTERED
            assert registered is not None
            assert registered.postgres.init_scripts == [], registered.postgres.init_scripts
        """
    )
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


REUSE_BOUND_PATCH = """
from unittest.mock import MagicMock
from pytest_testcontainers_django import containers as _containers
from pytest_testcontainers_django.containers import ContainerHandle


def _fake_pg_bound(**kwargs):
    return ContainerHandle(
        host="127.0.0.1",
        port=63403,
        name=kwargs.get("reuse_name"),
        is_bound_to_existing=True,
        _instance=MagicMock(),
        _owns_instance=False,
    )


_containers.start_postgres = _fake_pg_bound

from pytest_testcontainers_django import plugin as _plugin
_plugin.start_postgres = _fake_pg_bound
"""

STALE_REUSE_PATCH = """
from pytest_testcontainers_django import containers as _containers
from pytest_testcontainers_django.errors import ReuseStaleContainerError


def _fake_pg_stale(**kwargs):
    raise ReuseStaleContainerError(
        f"reuse-mode container {kwargs.get('reuse_name')!r} exists but is in state 'dead' — "
        f"it cannot be restarted and starting a fresh container would conflict on "
        f"the name. Remove it manually:\\n"
        f"  docker rm -f {kwargs.get('reuse_name')}"
    )


_containers.start_postgres = _fake_pg_stale

from pytest_testcontainers_django import plugin as _plugin
_plugin.start_postgres = _fake_pg_stale
"""


def test_reuse_mode_with_init_scripts_warns_on_bound_container(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC §10.7: reuse + init_scripts + bound-to-existing → stderr warning
    that init scripts are not replayed against the existing container.
    """
    sql = pytester.path / "baseline.sql"
    sql.write_text("-- baseline", encoding="utf-8")
    pytester.makepyprojecttoml(
        f"""
[tool.pytest-testcontainers-django]
postgres_database = "myapp"
postgres_init_scripts = ["{sql.name}"]
"""
    )
    pytester.makeconftest(REUSE_BOUND_PATCH)
    pytester.makepyfile("def test_dummy(): pass")

    monkeypatch.setenv("PYTEST_TESTCONTAINERS_REUSE", "1")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-s")
    combined = "\n".join([*result.outlines, *result.errlines])
    assert "init scripts NOT replayed" in combined, combined
    result.assert_outcomes(passed=1)


def test_reuse_mode_stale_container_raises_usage_error(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When reuse mode finds a dead/removing container, surface an
    actionable UsageError instead of letting Docker hit a 409 name conflict.
    """
    pytester.makepyprojecttoml("[tool.pytest-testcontainers-django]\n")
    pytester.makeconftest(STALE_REUSE_PATCH)
    pytester.makepyfile("def test_dummy(): pass")

    monkeypatch.setenv("PYTEST_TESTCONTAINERS_REUSE", "1")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.stderr.fnmatch_lines(["*docker rm -f*"])
    assert result.ret != 0


def test_invalid_init_script_path_fails_loudly(pytester: pytest.Pytester) -> None:
    _bootstrap(
        pytester,
        pyproject_extra="""
postgres_init_scripts = ["does/not/exist.sql"]
""",
    )
    pytester.makepyfile("def test_dummy(): pass")
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.stderr.fnmatch_lines(["*does not exist*"])
    assert result.ret != 0


def test_preload_quiet_when_conftest_hits_django_not_configured(
    pytester: pytest.Pytester,
) -> None:
    """Regression: when the user's conftest transitively imports code that
    touches Django settings before they're configured (e.g. ``model_bakery``
    >= 1.20 calling ``apps.is_installed`` at module import), our preload
    fails.  That failure is benign — pytest's normal trylast loader will
    re-import the conftest later, after our env injection and pytest-django
    have run — so we should NOT print a full traceback for it.

    The conftest below raises a locally-defined ``ImproperlyConfigured`` on
    its first import and lets the second (pytest core's) import succeed,
    matching the real-world flow.
    """
    sentinel = """
import sys
if not getattr(sys, "_ttd_preload_seen", False):
    sys._ttd_preload_seen = True
    class ImproperlyConfigured(Exception):
        pass
    raise ImproperlyConfigured(
        "Requested setting INSTALLED_APPS, but settings are not configured."
    )
"""
    _bootstrap(pytester, conftest_extra=sentinel)
    pytester.makepyfile("def test_dummy(): pass")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    combined = "\n".join([*result.outlines, *result.errlines])
    assert "Traceback" not in combined, combined
    assert "preloading rootdir conftest.py raised" not in combined, combined


def test_preload_still_logs_traceback_for_unrelated_conftest_errors(
    pytester: pytest.Pytester,
) -> None:
    """The quiet-on-ImproperlyConfigured shortcut must NOT swallow other
    conftest import errors.  A plain ``ValueError`` should still surface
    with a traceback (either from our own logger.exception during preload,
    or from pytest's trylast loader when it re-imports and fails again).
    """
    sentinel = """
raise ValueError("definitely not a django problem")
"""
    _bootstrap(pytester, conftest_extra=sentinel)
    pytester.makepyfile("def test_dummy(): pass")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    combined = "\n".join([*result.outlines, *result.errlines])
    assert "definitely not a django problem" in combined, combined
    assert result.ret != 0


def test_hook_runs_before_pytest_django_default_priority(
    pytester: pytest.Pytester,
) -> None:
    """Pluggy ordering smoke check: our hook is tryfirst, pytest-django's
    is default-priority — so ours must appear earlier in the resolved
    hook chain.

    We don't pull in Django itself; we inspect the hook caller list.
    """
    _bootstrap(pytester)
    pytester.makepyfile(
        """
        def test_ordering(request):
            from pytest_testcontainers_django import plugin as our_plugin

            hookcaller = request.config.pluginmanager.hook.pytest_load_initial_conftests
            order = [h.plugin for h in hookcaller.get_hookimpls()]
            our_index = order.index(our_plugin)
            try:
                import pytest_django.plugin as pdj_plugin
            except ImportError:
                # pytest-django not installed; the ordering invariant we
                # care about is simply "we are tryfirst" — verified below.
                assert our_index == len(order) - 1, order
                return
            pdj_index = order.index(pdj_plugin)
            # In pluggy, get_hookimpls returns trylast → default → tryfirst.
            # Higher index = earlier execution.
            assert our_index > pdj_index, (our_index, pdj_index, order)
        """
    )
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
