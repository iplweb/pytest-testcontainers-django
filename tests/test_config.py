"""Configuration loader / validator tests (SPEC §4-5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pytest_testcontainers_django import (
    DjangoContainerConfig,
    PostgresService,
    RedisService,
    register,
)
from pytest_testcontainers_django.config import (
    _reset_registered,
    apply_template_default,
    is_disabled,
    is_reuse_enabled,
    load_config,
    validate,
)
from pytest_testcontainers_django.errors import ConfigError


@pytest.fixture(autouse=True)
def _isolate_register():
    _reset_registered()
    yield
    _reset_registered()


def _write_pyproject(root: Path, body: str) -> None:
    (root / "pyproject.toml").write_text(body, encoding="utf-8")


def test_load_config_uses_defaults_when_no_pyproject(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert cfg.postgres.image == "postgres:16"
    assert cfg.postgres.env_names["host"] == "DJANGO_DB_HOST"
    assert cfg.redis is None
    assert cfg.disable_env == "PYTEST_TESTCONTAINERS_DISABLE"


def test_load_config_reads_pyproject_table(tmp_path: Path) -> None:
    _write_pyproject(
        tmp_path,
        """
[tool.pytest-testcontainers-django]
postgres_image = "iplweb/bpp_dbserver:psql-16.13"
postgres_user = "bpp"
postgres_password = "password"
postgres_database = "bpp"
postgres_template = "bpp"
postgres_init_scripts = ["fixtures/baseline.sql"]
postgres_env = { POSTGRESQL_UNSAFE_BUT_FAST = "1" }
db_host_env = "DJANGO_BPP_DB_HOST"
db_port_env = "DJANGO_BPP_DB_PORT"
db_name_env = "DJANGO_BPP_DB_NAME"
db_user_env = "DJANGO_BPP_DB_USER"
db_password_env = "DJANGO_BPP_DB_PASSWORD"
db_test_template_env = "DJANGO_BPP_TEST_TEMPLATE"
skip_dotenv_env = "DJANGO_BPP_SKIP_DOTENV"
disable_env = "BPP_USE_TESTCONTAINERS"
reuse_env = "BPP_TESTCONTAINERS_REUSE"

redis_enabled = true
redis_image = "redis:7-alpine"
redis_host_env = "DJANGO_BPP_REDIS_HOST"
redis_port_env = "DJANGO_BPP_REDIS_PORT"
""",
    )
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "baseline.sql").write_text("-- empty", encoding="utf-8")

    cfg = load_config(tmp_path)
    assert cfg.postgres.image == "iplweb/bpp_dbserver:psql-16.13"
    assert cfg.postgres.user == "bpp"
    assert cfg.postgres.template == "bpp"
    assert cfg.postgres.env_names["host"] == "DJANGO_BPP_DB_HOST"
    assert cfg.postgres.env == {"POSTGRESQL_UNSAFE_BUT_FAST": "1"}
    assert cfg.postgres.init_scripts == [tmp_path / "fixtures" / "baseline.sql"]
    assert cfg.skip_dotenv_env == "DJANGO_BPP_SKIP_DOTENV"
    assert cfg.disable_env == "BPP_USE_TESTCONTAINERS"

    assert cfg.redis is not None
    assert cfg.redis.image == "redis:7-alpine"
    assert cfg.redis.env_names["host"] == "DJANGO_BPP_REDIS_HOST"


def test_register_overrides_pyproject(tmp_path: Path) -> None:
    _write_pyproject(
        tmp_path,
        """
[tool.pytest-testcontainers-django]
postgres_image = "postgres:14"
""",
    )
    register(
        DjangoContainerConfig(
            postgres=PostgresService(image="postgres:17", database="myapp"),
        )
    )
    cfg = load_config(tmp_path)
    assert cfg.postgres.image == "postgres:17"
    assert cfg.postgres.database == "myapp"


def test_register_rejects_wrong_type() -> None:
    with pytest.raises(TypeError):
        register({"postgres_image": "postgres:16"})  # type: ignore[arg-type]


def test_apply_template_default_when_init_scripts_present(tmp_path: Path) -> None:
    sql = tmp_path / "baseline.sql"
    sql.write_text("-- empty", encoding="utf-8")
    cfg = DjangoContainerConfig(
        postgres=PostgresService(database="myapp", init_scripts=[sql]),
    )
    out = apply_template_default(cfg)
    assert out.postgres.template == "myapp"
    # input untouched
    assert cfg.postgres.template is None


def test_apply_template_default_skipped_when_no_init_scripts() -> None:
    cfg = DjangoContainerConfig(postgres=PostgresService(database="myapp"))
    out = apply_template_default(cfg)
    assert out.postgres.template is None


def test_apply_template_default_respects_explicit_value(tmp_path: Path) -> None:
    sql = tmp_path / "baseline.sql"
    sql.write_text("-- empty", encoding="utf-8")
    cfg = DjangoContainerConfig(
        postgres=PostgresService(database="myapp", template="seed_template", init_scripts=[sql]),
    )
    out = apply_template_default(cfg)
    assert out.postgres.template == "seed_template"


def test_validate_rejects_template_without_init_scripts() -> None:
    cfg = DjangoContainerConfig(postgres=PostgresService(template="myapp"))
    with pytest.raises(ConfigError, match="init_scripts is empty"):
        validate(cfg)


def test_validate_rejects_missing_init_script(tmp_path: Path) -> None:
    cfg = DjangoContainerConfig(
        postgres=PostgresService(init_scripts=[tmp_path / "nope.sql"]),
    )
    with pytest.raises(ConfigError, match="does not exist"):
        validate(cfg)


def test_validate_rejects_missing_redis_env_name() -> None:
    cfg = DjangoContainerConfig(
        redis=RedisService(env_names={"host": "DJANGO_REDIS_HOST", "port": ""})
    )
    with pytest.raises(ConfigError, match="env_names"):
        validate(cfg)


def test_validate_rejects_invalid_internal_port() -> None:
    cfg = DjangoContainerConfig(postgres=PostgresService(internal_port=0))
    with pytest.raises(ConfigError, match="internal_port"):
        validate(cfg)


def test_is_disabled_via_cli_flag() -> None:
    cfg = DjangoContainerConfig()
    assert is_disabled(cfg, ["--no-testcontainers"]) is True


def test_is_disabled_via_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = DjangoContainerConfig(disable_env="MYAPP_DISABLE")
    monkeypatch.setenv("MYAPP_DISABLE", "1")
    assert is_disabled(cfg, []) is True


def test_is_disabled_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = DjangoContainerConfig(disable_env="MYAPP_DISABLE")
    monkeypatch.delenv("MYAPP_DISABLE", raising=False)
    assert is_disabled(cfg, []) is False


def test_is_reuse_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = DjangoContainerConfig(reuse_env="MYAPP_REUSE")
    monkeypatch.setenv("MYAPP_REUSE", "yes")
    assert is_reuse_enabled(cfg) is True
    monkeypatch.setenv("MYAPP_REUSE", "0")
    assert is_reuse_enabled(cfg) is False
