"""Tests for configuration loading: defaults, TOML file, env overrides."""

from pathlib import Path

import pytest

from src import config as config_module
from src.config import (
    AppConfig,
    ModelConfig,
    SafetyConfig,
    get_config,
    get_default_safety_config,
    load_config,
)

ENV_VARS = ["OPENAI_API_KEY", "NL2CLI_MODEL", "NL2CLI_TEMPERATURE", "NL2CLI_LOG_LEVEL"]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point $HOME at a tmp dir and clear every nl2cli env var."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config_module, "_config", None)
    return tmp_path


def write_config(home: Path, body: str) -> Path:
    path = home / ".config" / "nl2cli" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


class TestDefaults:
    def test_model_defaults(self):
        model = ModelConfig()
        assert model.provider == "openai"
        assert model.temperature == 0.1
        assert model.timeout == 30

    def test_app_defaults(self):
        config = AppConfig()
        assert config.api_key is None
        assert config.log_level == "INFO"
        assert config.dry_run_default is False
        assert config.auto_backup is True
        assert config.confirm_mode == "auto"
        assert config.action_timeout == 30
        assert config.auto_install is False

    def test_shell_glue_is_disabled_by_default(self):
        assert SafetyConfig().allow_shell_glue is False

    def test_no_config_file_yields_defaults(self):
        assert load_config().model.model == ModelConfig().model


class TestConfigFile:
    def test_model_section_is_applied(self, isolated_env):
        write_config(isolated_env, '[model]\nmodel = "gpt-4o"\ntemperature = 0.5\n')
        config = load_config()
        assert config.model.model == "gpt-4o"
        assert config.model.temperature == 0.5

    def test_safety_section_is_applied(self, isolated_env):
        write_config(isolated_env, "[safety]\nallow_shell_glue = true\n")
        assert load_config().safety.allow_shell_glue is True

    def test_top_level_settings_are_applied(self, isolated_env):
        write_config(isolated_env, 'log_level = "DEBUG"\naction_timeout = 90\n')
        config = load_config()
        assert config.log_level == "DEBUG"
        assert config.action_timeout == 90

    def test_unknown_top_level_keys_are_ignored(self, isolated_env):
        write_config(isolated_env, 'nonsense_key = "x"\nlog_level = "DEBUG"\n')
        assert load_config().log_level == "DEBUG"

    def test_malformed_file_falls_back_to_defaults(self, isolated_env, capsys):
        write_config(isolated_env, "this is not = valid toml [[[")
        config = load_config()
        assert config.log_level == "INFO"
        assert "Could not load config file" in capsys.readouterr().out


class TestEnvironmentOverrides:
    def test_api_key_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert load_config().api_key == "sk-test"

    def test_model_override(self, monkeypatch):
        monkeypatch.setenv("NL2CLI_MODEL", "gpt-4o-mini")
        assert load_config().model.model == "gpt-4o-mini"

    def test_temperature_override(self, monkeypatch):
        monkeypatch.setenv("NL2CLI_TEMPERATURE", "0.7")
        assert load_config().model.temperature == 0.7

    def test_invalid_temperature_is_ignored(self, monkeypatch):
        monkeypatch.setenv("NL2CLI_TEMPERATURE", "hot")
        assert load_config().model.temperature == ModelConfig().temperature

    def test_log_level_is_upper_cased(self, monkeypatch):
        monkeypatch.setenv("NL2CLI_LOG_LEVEL", "debug")
        assert load_config().log_level == "DEBUG"

    def test_environment_beats_the_config_file(self, isolated_env, monkeypatch):
        write_config(isolated_env, '[model]\nmodel = "from-file"\n')
        monkeypatch.setenv("NL2CLI_MODEL", "from-env")
        assert load_config().model.model == "from-env"


class TestDefaultSafetyConfig:
    def test_dangerous_patterns_are_populated(self):
        patterns = get_default_safety_config().dangerous_patterns
        assert any("rm" in p for p in patterns)
        assert any("mkfs" in p for p in patterns)
        assert any("grub" in p for p in patterns)

    def test_destructive_binaries_are_denied(self):
        denied = get_default_safety_config().denied_binaries
        for binary in ["rm", "dd", "mkfs", "fdisk", "sudo"]:
            assert binary in denied

    def test_common_admin_binaries_are_allowed(self):
        allowed = get_default_safety_config().allowed_binaries
        for binary in ["systemctl", "journalctl", "apt", "grep"]:
            assert binary in allowed

    def test_allow_and_deny_lists_do_not_overlap_dangerously(self):
        safety = get_default_safety_config()
        overlap = set(safety.allowed_binaries) & set(safety.denied_binaries)
        # chmod/chown appear in neither role by accident; anything overlapping
        # would make the policy ambiguous.
        assert overlap == set()


class TestGetConfigCaching:
    def test_the_instance_is_memoised(self):
        assert get_config() is get_config()

    def test_the_cache_can_be_reset(self, monkeypatch):
        first = get_config()
        monkeypatch.setattr(config_module, "_config", None)
        assert get_config() is not first
