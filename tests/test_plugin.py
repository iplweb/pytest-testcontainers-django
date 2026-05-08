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
