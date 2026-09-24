"""Layered configuration for Settings: defaults < YAML file < env < overrides."""

from __future__ import annotations

import os

import pytest

from ctrlrtn.config import ConfigError, load_settings

_ENV_VARS = (
    "CTRLRTN_DB",
    "CTRLRTN_HOST",
    "CTRLRTN_PORT",
    "CTRLRTN_LOG_REQUESTS",
    "CTRLRTN_UPSTREAM",
    "CTRLRTN_ANTHROPIC_UPSTREAM",
    "CTRLRTN_OPENAI_UPSTREAM",
    "CTRLRTN_TIMEOUT",
    "CTRLRTN_INJECT_CACHE",
    "CTRLRTN_KILL_SWITCH",
    "CTRLRTN_RETENTION_DAYS",
    "CTRLRTN_CONFIG",
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Every test starts with a clean env and an empty cwd (no stray file)."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


def _config(tmp_path, monkeypatch, text):
    path = tmp_path / "cfg.yaml"
    path.write_text(text)
    monkeypatch.setenv("CTRLRTN_CONFIG", str(path))
    return path


# --- defaults --------------------------------------------------------------


def test_defaults_when_nothing_is_set():
    s = load_settings()
    assert os.path.basename(s.db_path) == "ctrlrtn.db"
    assert os.path.isabs(s.db_path)  # absolutized so the path is unambiguous
    assert s.host == "127.0.0.1"
    assert s.port == 4000
    assert s.log_requests is False
    assert s.log_level == "info"
    assert s.upstream_base_url is None
    assert s.timeout == 600.0
    assert s.inject_cache is False
    assert s.retention_days is None
    assert s.budget_policy.enabled is False


# --- inject_cache flag (behavior pinned before this feature) ---------------


def test_inject_cache_defaults_off():
    assert load_settings().inject_cache is False


def test_kill_switch_defaults_off_and_reads_the_environment(monkeypatch):
    assert load_settings().kill_switch is False
    monkeypatch.setenv("CTRLRTN_KILL_SWITCH", "true")
    assert load_settings().kill_switch is True


def test_retention_days_is_explicit_and_positive(tmp_path, monkeypatch):
    monkeypatch.setenv("CTRLRTN_RETENTION_DAYS", "30")
    assert load_settings().retention_days == 30

    monkeypatch.delenv("CTRLRTN_RETENTION_DAYS")
    _config(tmp_path, monkeypatch, "retention_days: 7\n")
    assert load_settings().retention_days == 7

    _config(tmp_path, monkeypatch, "retention_days: 0\n")
    with pytest.raises(ConfigError, match="retention_days must be at least 1"):
        load_settings()


def test_inject_cache_enabled_by_flag(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("CTRLRTN_INJECT_CACHE", value)
        assert load_settings().inject_cache is True


def test_inject_cache_off_for_other_values(monkeypatch):
    for value in ("0", "false", "no", ""):
        monkeypatch.setenv("CTRLRTN_INJECT_CACHE", value)
        assert load_settings().inject_cache is False


def test_flag_carries_into_single_upstream_mode(monkeypatch):
    monkeypatch.setenv("CTRLRTN_INJECT_CACHE", "1")
    monkeypatch.setenv("CTRLRTN_UPSTREAM", "http://example")
    settings = load_settings()
    assert settings.upstream_base_url == "http://example"
    assert settings.inject_cache is True


# --- env layer -------------------------------------------------------------


def test_env_sets_and_coerces(monkeypatch):
    monkeypatch.setenv("CTRLRTN_DB", "/data/r.db")
    monkeypatch.setenv("CTRLRTN_HOST", "0.0.0.0")
    monkeypatch.setenv("CTRLRTN_PORT", "8080")
    monkeypatch.setenv("CTRLRTN_TIMEOUT", "12.5")
    monkeypatch.setenv("CTRLRTN_LOG_REQUESTS", "on")
    s = load_settings()
    assert s.db_path == "/data/r.db"
    assert s.host == "0.0.0.0"
    assert s.port == 8080 and isinstance(s.port, int)
    assert s.timeout == 12.5
    assert s.log_requests is True


# --- YAML layer ------------------------------------------------------------


def test_yaml_provides_values_with_native_types(tmp_path, monkeypatch):
    _config(
        tmp_path,
        monkeypatch,
        "db_path: /var/r.db\nport: 9000\ninject_cache: true\n",
    )
    s = load_settings()
    assert s.db_path == "/var/r.db"
    assert s.port == 9000
    assert s.inject_cache is True


def test_default_config_file_is_discovered(tmp_path, monkeypatch):
    # No CTRLRTN_CONFIG; a ./ctrlrtn.yaml in cwd is picked up.
    (tmp_path / "ctrlrtn.yaml").write_text("port: 4321\n")
    assert load_settings().port == 4321


def test_single_upstream_via_yaml(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, "upstream: http://one-upstream\n")
    s = load_settings()
    assert s.upstream_base_url == "http://one-upstream"
    assert s.resolver().resolve("/anything") == "http://one-upstream"


def test_per_provider_upstreams_via_yaml(tmp_path, monkeypatch):
    _config(
        tmp_path,
        monkeypatch,
        "anthropic_upstream: http://my-anthropic\n",
    )
    s = load_settings()
    assert s.resolver().resolve("/v1/messages") == "http://my-anthropic"


def test_named_providers_augment_the_legacy_routes(tmp_path, monkeypatch):
    _config(
        tmp_path,
        monkeypatch,
        """providers:
  ollama:
    base_url: http://localhost:11434
    prefix: /ollama
    api: openai
    free: true
""",
    )

    resolver = load_settings().resolver()
    target = resolver.resolve_request("/ollama/v1/chat/completions")

    assert target is not None
    assert target.name == "ollama"
    assert target.api == "openai"
    assert target.free is True
    assert target.base_url == "http://localhost:11434"
    assert target.path == "/v1/chat/completions"
    assert resolver.resolve("/v1/chat/completions") == "https://api.openai.com"
    assert resolver.resolve("/v1/messages") == "https://api.anthropic.com"


def test_named_provider_defaults_to_its_name_as_prefix(tmp_path, monkeypatch):
    _config(
        tmp_path,
        monkeypatch,
        "providers:\n  local_llm:\n    base_url: http://localhost:11434\n",
    )

    target = (
        load_settings()
        .resolver()
        .resolve_request("/local_llm/v1/chat/completions")
    )

    assert target is not None
    assert target.name == "local_llm"
    assert target.path == "/v1/chat/completions"


def test_named_provider_reads_a_credential_descriptor(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "cloud-secret")
    _config(
        tmp_path,
        monkeypatch,
        """providers:
  ollama_cloud:
    base_url: https://ollama.com
    api: openai
    credential:
      env: OLLAMA_API_KEY
      header: authorization
      prefix: "Bearer "
""",
    )

    target = (
        load_settings()
        .resolver()
        .resolve_provider("ollama_cloud", "/v1/chat/completions")
    )

    assert target is not None
    assert target.credential is not None
    assert target.credential.headers() == {
        "authorization": "Bearer cloud-secret"
    }


def test_named_provider_rejects_an_unset_credential_env(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_CLOUD_TOKEN", raising=False)
    _config(
        tmp_path,
        monkeypatch,
        """providers:
  cloud:
    base_url: https://cloud.example
    credential:
      env: MISSING_CLOUD_TOKEN
""",
    )

    with pytest.raises(ConfigError, match="MISSING_CLOUD_TOKEN.*not set"):
        load_settings()


def test_yaml_parses_global_and_use_case_daily_budgets(tmp_path, monkeypatch):
    _config(
        tmp_path,
        monkeypatch,
        """budgets:
  reserve_in_flight: true
  global:
    daily_usd: 25
  session:
    limit_usd: 2
  use_cases:
    tag:editor:
      daily_usd: 5.5
      fallback_at_usd: 4
""",
    )

    policy = load_settings().budget_policy
    assert policy.reserve_in_flight is True
    assert policy.global_daily_usd == 25.0
    assert policy.session_limit_usd == 2.0
    assert policy.use_case_daily_usd == {"tag:editor": 5.5}
    assert policy.use_case_fallback_usd == {"tag:editor": 4.0}


@pytest.mark.parametrize(
    "yaml",
    [
        "budgets: []\n",
        "budgets:\n  global:\n    daily_usd: -1\n",
        "budgets:\n  global:\n    surprise: 1\n",
        "budgets:\n  session:\n    limit_usd: -1\n",
        "budgets:\n  session:\n    daily_usd: 1\n",
        "budgets:\n  use_cases: nope\n",
        "budgets:\n  use_cases:\n    tag:x:\n      daily_usd: nan\n",
        "budgets:\n  use_cases:\n    tag:x:\n      daily_usd: 1\n      fallback_at_usd: 1\n",
        "budgets:\n  use_cases:\n    tag:x:\n      fallback_at_usd: 0.5\n",
        "budgets:\n  reserve_in_flight: enabled\n",
        "budgets:\n  reserve_in_flight: true\n",
    ],
)
def test_invalid_budget_config_is_rejected(tmp_path, monkeypatch, yaml):
    _config(tmp_path, monkeypatch, yaml)
    with pytest.raises(ConfigError, match="budget"):
        load_settings()


@pytest.mark.parametrize(
    ("yaml", "message"),
    [
        ("providers: []\n", "providers.*mapping"),
        ("providers:\n  ollama: nope\n", "ollama.*mapping"),
        ("providers:\n  ollama:\n    prefix: /ollama\n", "base_url"),
        (
            "providers:\n  ollama:\n    base_url: http://o\n    surprise: x\n",
            "unknown.*surprise",
        ),
        (
            "providers:\n  ollama:\n    base_url: http://o\n    free: sometimes\n",
            "free.*boolean",
        ),
        (
            "providers:\n  ollama:\n    base_url: http://o\n    api: opneai\n",
            "api.*anthropic.*openai",
        ),
        (
            "providers:\n  one:\n    base_url: http://1\n    prefix: /same\n"
            "  two:\n    base_url: http://2\n    prefix: /same\n",
            "duplicate provider prefix",
        ),
        (
            "providers:\n  bad/name:\n    base_url: http://x\n",
            "provider name",
        ),
        (
            "providers:\n  local:\n    base_url: http://x\n    prefix: ctrlrtn\n",
            "prefix.*start with /",
        ),
        (
            "providers:\n  local:\n    base_url: http://x\n    prefix: /ctrlrtn\n",
            "reserved",
        ),
    ],
)
def test_invalid_named_provider_config_is_rejected(
    tmp_path, monkeypatch, yaml, message
):
    _config(tmp_path, monkeypatch, yaml)
    with pytest.raises(ConfigError, match=message):
        load_settings()


def test_single_upstream_and_named_providers_are_mutually_exclusive(
    tmp_path, monkeypatch
):
    _config(
        tmp_path,
        monkeypatch,
        """upstream: http://only
providers:
  ollama:
    base_url: http://localhost:11434
""",
    )
    with pytest.raises(ConfigError, match="upstream.*providers"):
        load_settings()


def test_unknown_yaml_key_is_rejected(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, "prot: 4000\n")  # typo for "port"
    with pytest.raises(ValueError, match="unknown config key"):
        load_settings()


def test_yaml_must_be_a_mapping(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, "- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        load_settings()


def test_empty_yaml_file_is_fine(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, "")
    assert load_settings().port == 4000  # falls through to default


def test_missing_explicit_config_file_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("CTRLRTN_CONFIG", str(tmp_path / "nope.yaml"))
    with pytest.raises(ConfigError, match="config file not found"):
        load_settings()


def test_non_scalar_yaml_value_is_rejected(tmp_path, monkeypatch):
    # A structure mistake must not become a garbage upstream URL.
    _config(tmp_path, monkeypatch, "upstream:\n  nested: 1\n")
    with pytest.raises(ConfigError, match="must be a scalar"):
        load_settings()


def test_bad_coercion_names_the_key_and_source(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, "port: not-a-number\n")
    with pytest.raises(ConfigError, match="port"):
        load_settings()


def test_bad_env_coercion_names_the_env_var(monkeypatch):
    monkeypatch.setenv("CTRLRTN_PORT", "abc")
    with pytest.raises(ConfigError, match="CTRLRTN_PORT"):
        load_settings()


def test_tilde_db_path_is_expanded_and_absolute(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, "db_path: ~/ctrlrtn.db\n")
    db_path = load_settings().db_path
    assert os.path.isabs(db_path) and "~" not in db_path


def test_cli_turns_a_bad_config_into_a_clean_failure(tmp_path, monkeypatch):
    from ctrlrtn.cli.commands import main

    _config(tmp_path, monkeypatch, "port: not-a-number\n")
    with pytest.raises(SystemExit) as exit_info:
        main(["experiment", "list"])  # a read command still fails cleanly
    assert exit_info.value.code == 2


# --- log level -------------------------------------------------------------


@pytest.fixture
def _restore_router_logger():
    import logging

    lg = logging.getLogger("ctrlrtn")
    saved = (list(lg.handlers), lg.level, lg.propagate)
    yield
    lg.handlers[:] = saved[0]
    lg.setLevel(saved[1])
    lg.propagate = saved[2]


def test_log_level_from_env(monkeypatch):
    monkeypatch.setenv("CTRLRTN_LOG_LEVEL", "debug")
    assert load_settings().log_level == "debug"


def test_configure_logging_sets_level_and_attaches_a_handler(
    _restore_router_logger,
):
    import logging

    from ctrlrtn.cli.commands import _configure_logging

    numeric = _configure_logging("debug")
    router_logger = logging.getLogger("ctrlrtn")
    assert numeric == logging.DEBUG
    assert router_logger.level == logging.DEBUG
    assert router_logger.handlers  # a formatted handler is attached


def test_configure_logging_rejects_an_unknown_level(_restore_router_logger):
    from ctrlrtn.cli.commands import _configure_logging

    with pytest.raises(SystemExit) as exit_info:
        _configure_logging("chatty")
    assert exit_info.value.code == 2


def test_null_yaml_value_falls_through_to_default(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, "db_path:\nport: 5000\n")  # db_path is null
    s = load_settings()
    assert os.path.basename(s.db_path) == "ctrlrtn.db"  # not literal "None"
    assert s.port == 5000


# --- precedence ------------------------------------------------------------


def test_env_overrides_yaml(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, "port: 1111\ndb_path: /from/yaml.db\n")
    monkeypatch.setenv("CTRLRTN_PORT", "2222")
    s = load_settings()
    assert s.port == 2222  # env wins
    assert s.db_path == "/from/yaml.db"  # yaml still applies where env is unset


def test_overrides_win_over_env(monkeypatch):
    monkeypatch.setenv("CTRLRTN_PORT", "2222")
    assert load_settings(overrides={"port": 9999}).port == 9999


def test_none_overrides_fall_through(monkeypatch):
    monkeypatch.setenv("CTRLRTN_HOST", "0.0.0.0")
    # An unset CLI flag arrives as None and must not clobber the env value.
    assert load_settings(overrides={"host": None}).host == "0.0.0.0"


# --- the CLI reads the same db_path the gateway would --------------------


def test_cli_honors_db_path_from_config_file(tmp_path, monkeypatch, capsys):
    from ctrlrtn.cli.commands import main

    db = tmp_path / "cli.db"
    _config(tmp_path, monkeypatch, f"db_path: {db}\n")
    main(["experiment", "start", "tag:x", "cheap-model", "--id", "exp:cfg"])
    capsys.readouterr()  # drop the "started" banner
    main(["experiment", "list"])
    assert "exp:cfg" in capsys.readouterr().out
    assert db.exists()  # wrote to the config's db_path, not ./ctrlrtn.db


def test_number_settings_reject_values_that_are_not_numbers(
    tmp_path, monkeypatch
):
    # YAML parses an unquoted date into a date object, a plausible typo.
    _config(tmp_path, monkeypatch, "port: 2026-01-01\n")
    with pytest.raises(ConfigError, match="expected a number, got date"):
        load_settings()
    _config(tmp_path, monkeypatch, "timeout: 2026-01-01\n")
    with pytest.raises(ConfigError, match="expected a number, got date"):
        load_settings()
