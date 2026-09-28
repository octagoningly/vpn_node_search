from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal, Mapping

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from nodebench.core.errors import ConfigError

MAX_YAML_BYTES = 2 * 1024 * 1024
MAX_YAML_DEPTH = 20
ENV_PREFIX = "NODEBENCH_"
SOURCES_FILENAME = "sources.yaml"

SECRET_KEY_EXACT = frozenset(
    {"secret", "secrets", "token", "password", "key", "api_key", "apikey"}
)
SECRET_KEY_SUFFIXES = ("_token", "_secret", "_password", "_key")
SECRET_ENV_EXACT = frozenset(
    {"token", "secret", "password", "key", "api_key", "apikey"}
)
SECRET_ENV_SUFFIXES = ("_token", "_secret", "_password", "_key")
PATH_SPLIT_PATTERN = re.compile(r"__|\.")


class LocalSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    paths: list[str] = Field(default_factory=list)


class SubscriptionSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    urls: list[str] = Field(default_factory=list)
    timeout_s: float = 15.0
    user_agent: str = "NodeBench/0.1 (+https://github.com/local/nodebench; bounded-research)"
    offline: bool = False
    license_tag: str = "unknown"

    @field_validator("timeout_s")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if float(value) <= 0:
            raise ValueError("must be greater than zero")
        return float(value)


class GithubSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    allowed_repositories: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    max_files_per_run: int = 30
    cache_ttl_hours: int = 24
    timeout_s: float = 15.0
    user_agent: str = "NodeBench/0.1 (+https://github.com/local/nodebench; bounded-research)"
    offline: bool = False

    @field_validator("timeout_s")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if float(value) <= 0:
            raise ValueError("must be greater than zero")
        return float(value)

    @field_validator("max_files_per_run", "cache_ttl_hours")
    @classmethod
    def _non_negative_counter(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("must be a non-negative integer")
        return value


class CfSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    candidate_files: list[str] = Field(default_factory=list)
    max_ips_per_run: int = 200



class CandidateImportConfig(BaseModel):
    """Dual-runtime import of already sorted / speed-tested candidate lists.

    Actions (or any other runner) can publish an ADDAPI txt or ADDCSV csv
    list; this machine imports it as CF candidates without re-running the
    remote collection. Historical remarks and speed values are kept as
    historical reference only.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    files: list[str] = Field(default_factory=list)
    license_tag: str = "user_supplied"
    max_ips_per_run: int = 200


class SourcesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local: LocalSourceConfig = Field(default_factory=LocalSourceConfig)
    subscriptions: SubscriptionSourceConfig = Field(
        default_factory=SubscriptionSourceConfig
    )
    github: GithubSourceConfig = Field(default_factory=GithubSourceConfig)
    cf: CfSourceConfig = Field(default_factory=CfSourceConfig)
    candidate_import: CandidateImportConfig = Field(
        default_factory=CandidateImportConfig
    )


class ProxyProbeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    concurrency: int = 10
    max_nodes: int = 100
    max_download_mb_each: float = 5
    speedtest_url: str = ""
    mihomo_path: str = ""
    api_timeout_s: float = 10
    per_node_timeout_s: float = 20
    total_deadline_s: float = 300

    @field_validator("api_timeout_s", "per_node_timeout_s", "total_deadline_s")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if float(value) <= 0:
            raise ValueError("must be greater than zero")
        return float(value)


class CfProbeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    concurrency: int = 20
    max_download_nodes: int = 20
    target_host: str = ""
    speedtest_url: str = ""
    cfst_path: str = ""
    tcp_timeout_s: float = 5
    tls_timeout_s: float = 5
    http_timeout_s: float = 5
    total_deadline_s: float = 600
    allowed_ports: list[int] = Field(default_factory=lambda: [443])
    ip_family: str = "4"

    @field_validator("tcp_timeout_s", "tls_timeout_s", "http_timeout_s", "total_deadline_s")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if float(value) <= 0:
            raise ValueError("must be greater than zero")
        return float(value)

    @field_validator("allowed_ports")
    @classmethod
    def _valid_ports(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("allowed_ports must not be empty")
        for port in value:
            if isinstance(port, bool) or not isinstance(port, int):
                raise ValueError("allowed_ports entries must be integers")
            if not 1 <= port <= 65535:
                raise ValueError("allowed_ports entries must be between 1 and 65535")
        return list(value)

    @field_validator("ip_family")
    @classmethod
    def _valid_ip_family(cls, value: str) -> str:
        if value not in {"4", "6", "all"}:
            raise ValueError("ip_family must be one of: 4, 6, all")
        return value


class ProbeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proxy: ProxyProbeConfig = Field(default_factory=ProxyProbeConfig)
    cf: CfProbeConfig = Field(default_factory=CfProbeConfig)


class IntelligenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reputation_enabled: bool = False
    echo_url: str = ""
    geo_url: str = ""
    reputation_url: str = ""
    timeout: float = 5.0

    @field_validator("timeout")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if float(value) <= 0:
            raise ValueError("must be greater than zero")
        return float(value)


class HistoryConfig(BaseModel):
    """History window and retention policy.

    ``days`` is the availability/statistics window (7/14-day rates).
    ``retention_days`` is how long runs are kept before ``prune_runs``
    deletes them; ``None`` keeps data as long as ``days``.
    """

    model_config = ConfigDict(extra="forbid")

    days: int = 14
    retention_days: int | None = None

    @field_validator("days", mode="before")
    @classmethod
    def _positive_days(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("history.days must be a positive integer")
        return value

    @field_validator("retention_days", mode="before")
    @classmethod
    def _valid_retention(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("history.retention_days must be a positive integer or null")
        return value


class ScoringWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latency: float = 0.25
    speed: float = 0.25
    purity: float = 0.25
    stability: float = 0.15
    loss: float = 0.10

    @field_validator("latency", "speed", "purity", "stability", "loss")
    @classmethod
    def _non_negative_weight(cls, value: float) -> float:
        number = float(value)
        if number < 0:
            raise ValueError("weights must not be negative")
        return number

    @model_validator(mode="after")
    def _weight_sum_positive(self) -> "ScoringWeights":
        total = (
            self.latency
            + self.speed
            + self.purity
            + self.stability
            + self.loss
        )
        if total <= 0:
            raise ValueError("at least one scoring weight must be positive")
        return self


class ScoringFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    require_real_probe_success: bool = True
    max_latency_ms: float = 800.0
    min_speed_mb_s: float = 0.5
    max_risk: float = 50.0
    allowed_countries: list[str] = Field(default_factory=list)
    missing: Literal["pending", "exclude"] = "pending"
    stability_min_samples: int = 3

    @field_validator("max_latency_ms")
    @classmethod
    def _positive_latency(cls, value: float) -> float:
        number = float(value)
        if number <= 0:
            raise ValueError("max_latency_ms must be greater than zero")
        return number

    @field_validator("min_speed_mb_s")
    @classmethod
    def _non_negative_speed(cls, value: float) -> float:
        number = float(value)
        if number < 0:
            raise ValueError("min_speed_mb_s must not be negative")
        return number

    @field_validator("max_risk")
    @classmethod
    def _valid_risk(cls, value: float) -> float:
        number = float(value)
        if not 0 <= number <= 100:
            raise ValueError("max_risk must be between 0 and 100")
        return number

    @field_validator("stability_min_samples")
    @classmethod
    def _counter_non_negative(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("stability_min_samples must not be negative")
        return value

    @field_validator("allowed_countries")
    @classmethod
    def _valid_countries(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            code = str(item).strip().upper()
            if len(code) != 2 or not code.isalpha():
                raise ValueError(
                    "allowed_countries entries must be ISO 3166-1 alpha-2 codes"
                )
            cleaned.append(code)
        return cleaned


class ScoringCfWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compatibility: float = 0.35
    latency: float = 0.25
    speed: float = 0.25
    loss: float = 0.15

    @field_validator("compatibility", "latency", "speed", "loss")
    @classmethod
    def _non_negative_weight(cls, value: float) -> float:
        number = float(value)
        if number < 0:
            raise ValueError("weights must not be negative")
        return number

    @model_validator(mode="after")
    def _weight_sum_positive(self) -> "ScoringCfWeights":
        total = self.compatibility + self.latency + self.speed + self.loss
        if total <= 0:
            raise ValueError("at least one cf scoring weight must be positive")
        return self


class ScoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    filters: ScoringFilters = Field(default_factory=ScoringFilters)
    cf_weights: ScoringCfWeights = Field(default_factory=ScoringCfWeights)


class GithubUploadConfig(BaseModel):
    """GitHub Contents API destination for gate-approved public files."""

    model_config = ConfigDict(extra="forbid")

    repository: str = ""
    branch: str = "public"
    path_prefix: str = "nodebench"
    token_env: str = "GITHUB_TOKEN"

    @field_validator("repository")
    @classmethod
    def _valid_repository(cls, value: str) -> str:
        text = str(value or "").strip()
        if text and text.count("/") != 1:
            raise ValueError("repository must look like owner/repo")
        return text


class HttpUploadConfig(BaseModel):
    """Generic HTTP PUT/POST destination for gate-approved public files."""

    model_config = ConfigDict(extra="forbid")

    url: str = ""
    method: Literal["PUT", "POST"] = "PUT"
    timeout_s: float = 15.0
    max_bytes: int = 2 * 1024 * 1024

    @field_validator("timeout_s")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if float(value) <= 0:
            raise ValueError("must be greater than zero")
        return float(value)

    @field_validator("max_bytes")
    @classmethod
    def _positive_max_bytes(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("max_bytes must be a positive integer")
        return value


class PublishUploadConfig(BaseModel):
    """Remote upload of the whitelisted public files.

    ``enabled`` defaults to False so existing runs stay local-only. The
    folder backend copies into ``folder_path`` for user-managed sync;
    github and http move files off the machine and require explicit
    destinations.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    backend: Literal["folder", "github", "http"] = "folder"
    folder_path: str = ""
    github: GithubUploadConfig = Field(default_factory=GithubUploadConfig)
    http: HttpUploadConfig = Field(default_factory=HttpUploadConfig)


class PublishConfig(BaseModel):
    """Gates for the public ``output/latest`` directory.

    ``allow_proxy_credentials`` releases proxy credential files into the
    public directory, and ``cf_candidates_authorized`` explicitly allows
    publishing files built from user imported CF candidates. Both default to
    False, as does ``enabled``. ``upload`` optionally moves the whitelisted
    public files to a remote destination after the local gates pass.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    allow_proxy_credentials: bool = False
    cf_candidates_authorized: bool = False
    upload: PublishUploadConfig = Field(default_factory=PublishUploadConfig)


class SchedulerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    default_time: str = "04:37"

    @field_validator("default_time")
    @classmethod
    def _valid_default_time(cls, value: str) -> str:
        text = str(value).strip()
        if not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", text):
            raise ValueError(
                "default_time must look like HH:MM between 00:00 and 23:59"
            )
        return text


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str = "local"
    runner_id: str = "local:desktop-a"
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    probe: ProbeConfig = Field(default_factory=ProbeConfig)
    intelligence: IntelligenceConfig = Field(default_factory=IntelligenceConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    budget: dict[str, float] = Field(default_factory=dict)
    output_dir: str = "output"
    secrets: dict[str, str] = Field(default_factory=dict, repr=False)


def _depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(item) for item in value), default=0)
    return 0


def _is_secret_key(key: Any) -> bool:
    lowered = str(key).lower()
    if lowered in SECRET_KEY_EXACT:
        return True
    return lowered.endswith(SECRET_KEY_SUFFIXES)


def _is_secret_env_name(name: str) -> bool:
    lowered = name.lower()
    if lowered in SECRET_ENV_EXACT:
        return True
    return lowered.endswith(SECRET_ENV_SUFFIXES)


def _reject_inline_secrets(value: Any, origin: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_secret_key(key):
                raise ConfigError(
                    code="inline_secret",
                    message=(
                        f"secret key '{key}' is not allowed in {origin}; "
                        f"use {ENV_PREFIX} environment variables instead"
                    ),
                )
            _reject_inline_secrets(item, origin)
    elif isinstance(value, list):
        for item in value:
            _reject_inline_secrets(item, origin)


def _load_yaml_layer(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(code="config_missing", message=f"config file not found: {path}")
    size = path.stat().st_size
    if size > MAX_YAML_BYTES:
        raise ConfigError(
            code="yaml_too_large",
            message=f"config file exceeds {MAX_YAML_BYTES} bytes: {path}",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise ConfigError(
            code="config_unreadable", message=f"config file cannot be read: {path}"
        ) from err
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ConfigError(
            code="yaml_invalid", message=f"invalid yaml in {path}: {err}"
        ) from err
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(
            code="yaml_invalid", message=f"config root must be a mapping: {path}"
        )
    depth = _depth(loaded)
    if depth > MAX_YAML_DEPTH:
        raise ConfigError(
            code="yaml_depth_exceeded",
            message=f"config nesting depth {depth} exceeds limit {MAX_YAML_DEPTH}: {path}",
        )
    _reject_inline_secrets(loaded, str(path))
    return loaded


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _split_path(path: str) -> list[str]:
    return [part for part in PATH_SPLIT_PATTERN.split(path) if part]


def _set_path(data: dict[str, Any], path: str, value: Any, origin: str) -> None:
    parts = _split_path(path)
    if not parts:
        raise ConfigError(
            code="override_empty_path", message=f"empty override path from {origin}"
        )
    cursor: dict[str, Any] = data
    for part in parts[:-1]:
        existing = cursor.get(part)
        if existing is None:
            nested: dict[str, Any] = {}
            cursor[part] = nested
            cursor = nested
        elif isinstance(existing, dict):
            cursor = existing
        else:
            raise ConfigError(
                code="override_conflict",
                message=(
                    f"cannot apply override from {origin}: "
                    f"'{'.'.join(parts)}' conflicts at '{part}'"
                ),
            )
    cursor[parts[-1]] = value


def _parse_env_value(raw: str, name: str) -> Any:
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as err:
        raise ConfigError(
            code="env_value_invalid",
            message=f"cannot parse value of {name} as a yaml scalar",
        ) from err
    if value is None and raw == "":
        return ""
    return value


def _apply_env(data: dict[str, Any], env: Mapping[str, str]) -> None:
    secrets_map: dict[str, str] = {}
    for name in env:
        if not name.startswith(ENV_PREFIX):
            continue
        rest = name[len(ENV_PREFIX) :]
        if not rest:
            continue
        key = rest.lower()
        if _is_secret_env_name(key):
            secrets_map[key] = str(env[name])
            continue
        value = _parse_env_value(str(env[name]), name)
        _set_path(data, key, value, origin=name)
    if secrets_map:
        existing = data.get("secrets")
        if not isinstance(existing, dict):
            existing = {}
            data["secrets"] = existing
        existing.update(secrets_map)


def _validate(data: dict[str, Any]) -> AppConfig:
    try:
        return AppConfig.model_validate(data)
    except ValidationError as err:
        errors = err.errors()
        first = errors[0] if errors else {"loc": (), "msg": "invalid configuration"}
        location = ".".join(str(part) for part in first.get("loc", ()))
        message = str(first.get("msg", "invalid value"))
        detail = f"{location}: {message}" if location else message
        raise ConfigError(code="config_invalid", message=f"invalid config: {detail}") from err


def _check_cf(config: AppConfig) -> None:
    cf_enabled = config.sources.cf.enabled or config.probe.cf.enabled
    target_host = (config.probe.cf.target_host or "").strip()
    if cf_enabled and not target_host:
        raise ConfigError(
            code="cf_target_host_missing",
            message=(
                "probe.cf.target_host is required when cf is enabled but is empty; "
                "set the target host or disable sources.cf and probe.cf"
            ),
        )


def load_config(
    default_path: str | os.PathLike[str],
    profile_path: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> AppConfig:
    """Merge default, sources layer, profile, environment secrets and CLI overrides."""
    default_file = Path(default_path)
    data = _load_yaml_layer(default_file)
    sources_file = default_file.parent / SOURCES_FILENAME
    if sources_file.is_file():
        data = _deep_merge(data, _load_yaml_layer(sources_file))
    if profile_path is not None:
        data = _deep_merge(data, _load_yaml_layer(Path(profile_path)))
    env_map = os.environ if env is None else env
    _apply_env(data, env_map)
    if cli_overrides:
        _reject_inline_secrets(dict(cli_overrides), "cli overrides")
        for key, value in cli_overrides.items():
            _set_path(data, str(key), value, origin="cli")
    config = _validate(data)
    _check_cf(config)
    return config


__all__ = [
    "MAX_YAML_BYTES",
    "MAX_YAML_DEPTH",
    "ENV_PREFIX",
    "AppConfig",
    "LocalSourceConfig",
    "SubscriptionSourceConfig",
    "GithubSourceConfig",
    "CfSourceConfig",
    "CandidateImportConfig",
    "SourcesConfig",
    "ProxyProbeConfig",
    "CfProbeConfig",
    "ProbeConfig",
    "IntelligenceConfig",
    "HistoryConfig",
    "ScoringWeights",
    "ScoringFilters",
    "ScoringCfWeights",
    "ScoringConfig",
    "GithubUploadConfig",
    "HttpUploadConfig",
    "PublishUploadConfig",
    "PublishConfig",
    "SchedulerConfig",
    "load_config",
]
