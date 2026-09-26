from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from nodebench.core.config import AppConfig, load_config
from nodebench.core.errors import ConfigError
from nodebench.core.serialization import public_dump

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_PATH = CONFIG_DIR / "default.yaml"
PROFILE_DIR = CONFIG_DIR / "profiles"

SAMPLE_FILE_MB = 2 * 1024 * 1024


def write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def base_default(tmp_path: Path) -> Path:
    return write_yaml(
        tmp_path / "default.yaml",
        {
            "profile": "local",
            "runner_id": "local:desktop-a",
            "history": {"days": 14},
            "intelligence": {"reputation_enabled": False},
            "probe": {
                "proxy": {"enabled": True},
                "cf": {"enabled": False, "target_host": ""},
            },
            "sources": {"cf": {"enabled": False}},
        },
    )


def test_default_yaml_loads():
    config = load_config(DEFAULT_PATH, None, env={})
    assert isinstance(config, AppConfig)
    assert config.profile == "local"
    assert config.runner_id == "local:desktop-a"
    assert config.output_dir == "output"
    assert config.budget == {}
    assert config.probe.proxy.enabled is True
    assert config.probe.proxy.speedtest_url == ""
    assert config.probe.cf.enabled is False
    assert config.probe.cf.target_host == ""
    assert config.probe.cf.speedtest_url == ""
    assert config.history.days == 14
    assert config.publish.allow_proxy_credentials is False


@pytest.mark.parametrize("name", ["local", "github", "cf-only", "proxy-only"])
def test_shipped_profiles_load(name: str):
    config = load_config(DEFAULT_PATH, PROFILE_DIR / f"{name}.yaml", env={})
    assert config.profile == name


def test_cf_only_disables_proxy_probe():
    config = load_config(DEFAULT_PATH, PROFILE_DIR / "cf-only.yaml", env={})
    assert config.probe.proxy.enabled is False


def test_proxy_only_disables_cf_source():
    config = load_config(DEFAULT_PATH, PROFILE_DIR / "proxy-only.yaml", env={})
    assert config.sources.cf.enabled is False
    assert config.probe.cf.enabled is False


def test_sources_yaml_layer_exists_beside_default():
    assert (CONFIG_DIR / "sources.yaml").is_file()


def test_default_layer_applies_without_profile(tmp_path: Path):
    config = load_config(base_default(tmp_path), None, env={})
    assert config.history.days == 14
    assert config.profile == "local"


def test_sources_layer_overrides_default(tmp_path: Path):
    default = base_default(tmp_path)
    write_yaml(tmp_path / "sources.yaml", {"history": {"days": 21}})
    config = load_config(default, None, env={})
    assert config.history.days == 21


def test_profile_overrides_sources_layer(tmp_path: Path):
    default = base_default(tmp_path)
    write_yaml(tmp_path / "sources.yaml", {"history": {"days": 21}})
    profile = write_yaml(tmp_path / "profile.yaml", {"history": {"days": 30}})
    config = load_config(default, profile, env={})
    assert config.history.days == 30
    assert config.profile == "local"
    assert config.runner_id == "local:desktop-a"


def test_env_overrides_profile(tmp_path: Path):
    default = base_default(tmp_path)
    profile = write_yaml(tmp_path / "profile.yaml", {"history": {"days": 30}})
    config = load_config(
        default, profile, env={"NODEBENCH_HISTORY__DAYS": "45"}
    )
    assert config.history.days == 45


def test_cli_overrides_env(tmp_path: Path):
    default = base_default(tmp_path)
    profile = write_yaml(tmp_path / "profile.yaml", {"history": {"days": 30}})
    config = load_config(
        default,
        profile,
        env={"NODEBENCH_HISTORY__DAYS": "45"},
        cli_overrides={"history.days": 60},
    )
    assert config.history.days == 60


def test_env_scalar_parsing(tmp_path: Path):
    default = base_default(tmp_path)
    config = load_config(
        default,
        None,
        env={
            "NODEBENCH_INTELLIGENCE__REPUTATION_ENABLED": "true",
            "NODEBENCH_PROFILE": "github",
            "NODEBENCH_OUTPUT_DIR": "out/runs",
        },
    )
    assert config.intelligence.reputation_enabled is True
    assert config.profile == "github"
    assert config.output_dir == "out/runs"


def test_non_prefixed_env_is_ignored(tmp_path: Path):
    default = base_default(tmp_path)
    config = load_config(default, None, env={"GITHUB_TOKEN": "ghp_example_token_value"})
    assert config.secrets == {}
    assert config.profile == "local"


def test_unknown_env_field_raises(tmp_path: Path):
    default = base_default(tmp_path)
    with pytest.raises(ConfigError) as excinfo:
        load_config(default, None, env={"NODEBENCH_NOT_A_FIELD": "1"})
    assert "not_a_field" in str(excinfo.value)


def test_unknown_profile_field_raises(tmp_path: Path):
    default = base_default(tmp_path)
    profile = write_yaml(tmp_path / "profile.yaml", {"surprise_field": 1})
    with pytest.raises(ConfigError) as excinfo:
        load_config(default, profile, env={})
    assert "surprise_field" in str(excinfo.value)


def test_missing_default_file_raises(tmp_path: Path):
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "absent.yaml", None, env={})
    assert "absent.yaml" in str(excinfo.value)


def test_missing_profile_file_raises(tmp_path: Path):
    default = base_default(tmp_path)
    with pytest.raises(ConfigError) as excinfo:
        load_config(default, tmp_path / "absent-profile.yaml", env={})
    assert "absent-profile.yaml" in str(excinfo.value)


def test_yaml_depth_limit(tmp_path: Path):
    deep: dict = {"leaf": "x"}
    for _ in range(21):
        deep = {"nested": deep}
    path = write_yaml(tmp_path / "default.yaml", {"profile": "local", "probe": deep})
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, None, env={})
    assert "depth" in str(excinfo.value)


def test_yaml_size_limit(tmp_path: Path):
    path = tmp_path / "default.yaml"
    path.write_text(
        yaml.safe_dump(
            {"profile": "local", "runner_id": "x" * (SAMPLE_FILE_MB + 64)},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, None, env={})
    assert "bytes" in str(excinfo.value)


def test_cf_probe_enabled_without_target_host_raises(tmp_path: Path):
    default = base_default(tmp_path)
    profile = write_yaml(tmp_path / "profile.yaml", {"probe": {"cf": {"enabled": True}}})
    with pytest.raises(ConfigError) as excinfo:
        load_config(default, profile, env={})
    assert "probe.cf.target_host" in str(excinfo.value)


def test_cf_source_enabled_without_target_host_raises(tmp_path: Path):
    default = base_default(tmp_path)
    profile = write_yaml(
        tmp_path / "profile.yaml", {"sources": {"cf": {"enabled": True}}}
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(default, profile, env={})
    assert "probe.cf.target_host" in str(excinfo.value)


def test_cf_enabled_through_env_without_target_host_raises(tmp_path: Path):
    default = base_default(tmp_path)
    with pytest.raises(ConfigError) as excinfo:
        load_config(default, None, env={"NODEBENCH_PROBE__CF__ENABLED": "true"})
    assert "probe.cf.target_host" in str(excinfo.value)


def test_cf_enabled_with_target_host_loads(tmp_path: Path):
    default = base_default(tmp_path)
    profile = write_yaml(
        tmp_path / "profile.yaml",
        {"probe": {"cf": {"enabled": True, "target_host": "speed.example.test"}}},
    )
    config = load_config(default, profile, env={})
    assert config.probe.cf.enabled is True
    assert config.probe.cf.target_host == "speed.example.test"


def test_secrets_come_only_from_env(tmp_path: Path):
    default = base_default(tmp_path)
    config = load_config(
        default,
        None,
        env={
            "NODEBENCH_GITHUB_TOKEN": "ghp_example_token_value",
            "NODEBENCH_REPUTATION_API_KEY": "rep_example_key_value",
        },
    )
    assert config.secrets == {
        "github_token": "ghp_example_token_value",
        "reputation_api_key": "rep_example_key_value",
    }
    dumped = public_dump(config)
    assert "secrets" not in dumped
    assert "ghp_example_token_value" not in str(dumped)
    assert "rep_example_key_value" not in str(dumped)


def test_inline_secret_key_in_yaml_raises(tmp_path: Path):
    path = write_yaml(
        tmp_path / "default.yaml",
        {
            "profile": "local",
            "sources": {"github": {"enabled": False, "token": "ghp_example_token_value"}},
        },
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, None, env={})
    assert "token" in str(excinfo.value)


def test_cli_cannot_inject_secrets(tmp_path: Path):
    default = base_default(tmp_path)
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            default, None, env={}, cli_overrides={"secrets.github_token": "abc12345"}
        )
    assert "secrets.github_token" in str(excinfo.value) or "github_token" in str(
        excinfo.value
    )


def test_app_config_rejects_extra_fields():
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"profile": "local", "surprise": True})
