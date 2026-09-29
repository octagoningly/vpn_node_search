from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1
FINGERPRINT_VERSION = 1

RUN_ID_PATTERN = r"^\d{8}T\d{6}Z-[0-9a-f]{6}$"

USER_SUPPLIED_LICENSE_TAG = "user_supplied"
USER_SUPPLIED_LICENSE_TAGS = frozenset(
    {USER_SUPPLIED_LICENSE_TAG, "not_licensed_user_supplied"}
)
PRIVATE_VISIBILITY = "private"
PUBLIC_VISIBILITY = "public"


class Status(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    STALE = "stale"


class Kind(str, Enum):
    PROXY_NODE = "proxy_node"
    EDGE_ENDPOINT = "edge_endpoint"


class ProtocolSupport(str, Enum):
    """How far a protocol's detection capability has been verified.

    判定规则（开发规则 §1 交付阶段）：某协议只有在真实连通测试通过后
    才能标为 ``probe_supported``（支持检测），否则标为 ``parse_only``
    （可解析）。解析/规范化阶段一律先标 ``parse_only``；只有当次运行
    出现该协议 ``probe_mode=real`` 且 ``status=ok`` 的探测证据后，
    报告中的协议级别才能提升为 ``probe_supported``。TCP 入口可达、
    模拟（simulated）或未执行（not_run）结果均不构成证据。
    """

    PARSE_ONLY = "parse_only"
    PROBE_SUPPORTED = "probe_supported"


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    code: str
    message_redacted: str
    retryable: bool = False


class RunContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=RUN_ID_PATTERN)
    runner_id: str
    profile: str
    budget: dict[str, float] = Field(default_factory=dict)
    deadline: datetime | None = None

    @classmethod
    def create(
        cls,
        profile: str,
        runner_id: str,
        budget: dict[str, float] | None = None,
        deadline: datetime | None = None,
        now: datetime | None = None,
    ) -> RunContext:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
        stamp = moment.strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{stamp}-{secrets.token_hex(3)}"
        return cls(
            run_id=run_id,
            runner_id=runner_id,
            profile=profile,
            budget=dict(budget or {}),
            deadline=deadline,
        )


class RawItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    content_type: Literal["uri_list", "base64_sub", "yaml", "csv", "text", "endpoint_list"]
    payload: str
    fetched_at: datetime
    license_tag: str = "unknown"
    source_ref: str = ""
    item_id: str | None = None


class ParseIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    code: str
    message_redacted: str
    raw_ref: str = ""


class ParsedProxy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    protocol: str
    server: str
    port: int
    transport: str = "tcp"
    security: str = "none"
    params: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict, repr=False)
    remarks: str = ""
    # parse_only until a real connectivity test passes for this protocol.
    protocol_support: ProtocolSupport = ProtocolSupport.PARSE_ONLY


class ParsedEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    address: str
    port: int
    target_host: str = ""
    tls: bool = False
    params: dict[str, Any] = Field(default_factory=dict)
    remarks: str = ""


class ProxyNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    kind: Literal["proxy_node"]
    fingerprint: str
    fingerprint_version: int
    protocol: str
    server: str
    port: int
    transport: str
    security: str
    params: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict, repr=False)
    remarks: str = ""
    source_ids: list[str] = Field(default_factory=list)
    raw_refs: list[str] = Field(default_factory=list)
    # parse_only until a real connectivity test passes for this protocol.
    protocol_support: ProtocolSupport = ProtocolSupport.PARSE_ONLY


class EdgeEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    kind: Literal["edge_endpoint"]
    fingerprint: str
    fingerprint_version: int
    address: str
    port: int
    target_host: str = ""
    tls: bool = False
    params: dict[str, Any] = Field(default_factory=dict)
    remarks: str = ""
    source_ids: list[str] = Field(default_factory=list)
    raw_refs: list[str] = Field(default_factory=list)


class SourceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    ok: bool
    fetched: int = 0
    errors: list[ErrorInfo] = Field(default_factory=list)
    scope: str = ""
    redacted: bool = True
    mode: str = ""
    etag: str = ""
    last_modified: str = ""


class ProbeStatus(str, Enum):
    OK = "ok"
    FAIL = "fail"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
    MEASUREMENT_ERROR = "measurement_error"
    INCOMPATIBLE = "incompatible"


class FailureStage(str, Enum):
    PROCESS = "process"
    CONFIG = "config"
    DNS = "dns"
    TCP = "tcp"
    TLS = "tls"
    HTTP = "http"
    DOWNLOAD = "download"
    UNKNOWN = "unknown"


class ProbeMode(str, Enum):
    REAL = "real"
    SIMULATED = "simulated"
    NOT_RUN = "not_run"


PROBE_ERROR_CODES = frozenset(
    {
        "config_error",
        "source_error",
        "parse_error",
        "probe_error",
        "storage_error",
        "export_error",
        "invalid_row",
    }
)

# Probe skip vocabulary shared by probes, publishing gates and reports.
REASON_DISABLED = "disabled"
REASON_MISSING_BINARY = "missing_binary"
REASON_MISSING_SPEEDTEST_URL = "missing_speedtest_url"
REASON_MISSING_TARGET_HOST = "missing_target_host"
REASON_BUDGET_EXHAUSTED = "budget_exhausted"
REASON_LIMIT_EXCEEDED = "probe_limit_exceeded"
REASON_NOT_RUN = "not_run"
REASON_DRY_RUN = "dry_run"
REASON_NO_CANDIDATES = "no_candidates"

SKIP_REASONS = frozenset(
    {
        REASON_DISABLED,
        REASON_MISSING_BINARY,
        REASON_MISSING_SPEEDTEST_URL,
        REASON_MISSING_TARGET_HOST,
        REASON_BUDGET_EXHAUSTED,
        REASON_LIMIT_EXCEEDED,
    }
)
CAPABILITY_SKIP_REASONS = frozenset(
    {
        REASON_MISSING_BINARY,
        REASON_MISSING_SPEEDTEST_URL,
        REASON_MISSING_TARGET_HOST,
        REASON_BUDGET_EXHAUSTED,
        REASON_LIMIT_EXCEEDED,
    }
)

# CF addcsv column contract (WorkerVless2sub 9-column header).
HEADER_FULL = [
    "IP地址",
    "端口",
    "回源端口",
    "TLS",
    "数据中心",
    "地区",
    "城市",
    "TCP延迟(ms)",
    "速度(MB/s)",
]
HEADER_TWO = ["ip", "port"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _check_port(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("port must be an integer")
    if not 1 <= value <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return value


def _check_counter(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


class ProbeResultBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    run_id: str
    runner_id: str
    measured_at: datetime = Field(default_factory=_utc_now)
    status: ProbeStatus
    failure_stage: FailureStage = FailureStage.UNKNOWN
    probe_mode: ProbeMode
    backend: str
    backend_version: str = ""
    skipped_reason: str = ""
    attempts: int = 0
    timeouts: int = 0
    error_code: str = ""
    error_message: str = ""
    notes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("backend")
    @classmethod
    def _backend_not_empty(cls, value: str) -> str:
        if not str(value).strip():
            raise ValueError("backend must not be empty")
        return value

    @field_validator("error_code")
    @classmethod
    def _error_code_known(cls, value: str) -> str:
        if value and value not in PROBE_ERROR_CODES:
            raise ValueError(f"unknown error_code: {value}")
        return value

    @field_validator("attempts", "timeouts")
    @classmethod
    def _counter_non_negative(cls, value: int) -> int:
        return _check_counter("counter", value)

    @model_validator(mode="after")
    def _status_invariants(self) -> "ProbeResultBase":
        if self.timeouts > self.attempts:
            raise ValueError("timeouts must not exceed attempts")
        if self.status is ProbeStatus.SKIPPED and not self.skipped_reason.strip():
            raise ValueError("skipped results require a skipped_reason")
        if self.status is ProbeStatus.OK and self.failure_stage is not FailureStage.UNKNOWN:
            raise ValueError("status=ok requires failure_stage=unknown")
        return self


class ProxyProbeResult(ProbeResultBase):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["proxy_probe_result"] = "proxy_probe_result"
    item_id: str
    dns_ms: float | None = None
    tcp_ms: float | None = None
    tls_ms: float | None = None
    ttfb_ms: float | None = None
    total_latency_ms: float | None = None
    download_bytes: int = 0
    speed_mbps: float | None = None
    speed_mb_s: float | None = None
    speed_unit: str = "MB/s"
    proxy_exit_ip: str = ""
    probe_url_host: str = ""
    measured_via: str = ""

    @field_validator("download_bytes")
    @classmethod
    def _download_bytes_non_negative(cls, value: int) -> int:
        return _check_counter("download_bytes", value)

    @model_validator(mode="after")
    def _speed_requires_bytes(self) -> "ProxyProbeResult":
        if self.download_bytes == 0 and (self.speed_mb_s is not None or self.speed_mbps is not None):
            raise ValueError("speed measurements require downloaded bytes")
        if not self.speed_unit.strip():
            raise ValueError("speed_unit must not be empty")
        return self


class EndpointProbeResult(ProbeResultBase):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["endpoint_probe_result"] = "endpoint_probe_result"
    item_id: str
    address: str
    port: int
    target_host: str = ""
    tls: bool = False
    tcp_ok: bool | None = None
    https_ok: bool | None = None
    host_compatible: bool | None = None
    loss_pct: float | None = None
    latency_ms: float | None = None
    speed_mb_s: float | None = None
    speed_unit: str = "MB/s"
    region: str = ""
    sent_bytes: int | None = None
    recv_bytes: int | None = None
    csv_row: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _endpoint_invariants(self) -> "EndpointProbeResult":
        _check_port(self.port)
        if not self.address.strip():
            raise ValueError("address must not be empty")
        if not self.speed_unit.strip():
            raise ValueError("speed_unit must not be empty")
        for name, value in (("sent_bytes", self.sent_bytes), ("recv_bytes", self.recv_bytes)):
            if value is not None and int(value) < 0:
                raise ValueError(f"{name} must not be negative")
        return self


SCORE_STATUSES = ("ranked", "filtered", "pending")


class HistorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    window_days: int
    scheduled: int = 0
    executed: int = 0
    succeeded: int = 0
    sample_count: int = 0
    availability_rate: float | None = None
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None
    min_samples: int = 0

    @field_validator(
        "window_days",
        "scheduled",
        "executed",
        "succeeded",
        "sample_count",
        "min_samples",
    )
    @classmethod
    def _counter_non_negative(cls, value: int) -> int:
        return _check_counter("counter", value)

    @model_validator(mode="after")
    def _history_invariants(self) -> "HistorySummary":
        if self.window_days < 1:
            raise ValueError("window_days must be at least 1")
        if self.executed > self.scheduled:
            raise ValueError("executed must not exceed scheduled")
        if self.succeeded > self.executed:
            raise ValueError("succeeded must not exceed executed")
        if self.sample_count > self.succeeded:
            raise ValueError("sample_count must not exceed succeeded")
        if (
            self.availability_rate is not None
            and not 0.0 <= self.availability_rate <= 1.0
        ):
            raise ValueError("availability_rate must be between 0 and 1")
        return self


class SourceQuality(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    runs_seen: int = 0
    ok_runs: int = 0
    fetched_total: int = 0
    error_total: int = 0

    @field_validator("runs_seen", "ok_runs", "fetched_total", "error_total")
    @classmethod
    def _counter_non_negative(cls, value: int) -> int:
        return _check_counter("counter", value)


class PersistSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "failed", "skipped"]
    reason: str = ""
    database: str = ""
    runs: int = 0
    sources: int = 0
    items: int = 0
    observations: int = 0
    pruned: int = 0
    source_quality: list[SourceQuality] = Field(default_factory=list)
    error: str = ""

    @field_validator("runs", "sources", "items", "observations", "pruned")
    @classmethod
    def _counter_non_negative(cls, value: int) -> int:
        return _check_counter("counter", value)


class ScoreIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    kind: Literal["proxy_node", "edge_endpoint"]
    code: str
    message_redacted: str = ""


UNKNOWN_PLACEHOLDER = "unknown"


class ExitObservation(BaseModel):
    """Geo/ASN/ISP snapshot for one proxy exit IP."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    exit_ip: str = ""
    service: str = ""
    observed_at: datetime = Field(default_factory=_utc_now)
    status: Status = Status.UNKNOWN
    country_code: str = UNKNOWN_PLACEHOLDER
    asn: str = UNKNOWN_PLACEHOLDER
    isp: str = UNKNOWN_PLACEHOLDER
    errors: list[ErrorInfo] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exit_invariants(self) -> "ExitObservation":
        if self.status is Status.OK and not self.exit_ip.strip():
            raise ValueError("status=ok requires a non-empty exit_ip")
        return self


class ReputationObservation(BaseModel):
    """Optional reputation-provider snapshot for one exit IP.

    ``risk`` is the provider score normalized to 0-100 (higher = riskier).
    It stays ``None`` when the provider is disabled or failed so purity is
    never fabricated.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    exit_ip: str = ""
    provider: str = ""
    raw_score: float | None = None
    risk: float | None = None
    risk_level: str = UNKNOWN_PLACEHOLDER
    evidence: str = ""
    observed_at: datetime = Field(default_factory=_utc_now)
    status: Status = Status.UNKNOWN
    errors: list[ErrorInfo] = Field(default_factory=list)

    @field_validator("raw_score", "risk")
    @classmethod
    def _score_bounds(cls, value: float | None, info: Any) -> float | None:
        if value is None:
            return None
        number = float(value)
        if info.field_name == "risk" and not 0.0 <= number <= 100.0:
            raise ValueError("risk must be between 0 and 100")
        return number

    @model_validator(mode="after")
    def _reputation_invariants(self) -> "ReputationObservation":
        if self.status is Status.OK and self.risk is None:
            raise ValueError("status=ok requires a risk score")
        return self


class IntelligenceReport(BaseModel):
    """Aggregated exit + reputation intelligence for one run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    runner_id: str
    generated_at: datetime = Field(default_factory=_utc_now)
    counts: dict[str, int] = Field(default_factory=dict)
    entries: list[ExitObservation] = Field(default_factory=list)
    reputations: list[ReputationObservation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _counts_non_negative(self) -> "IntelligenceReport":
        for name, value in self.counts.items():
            _check_counter(name, value)
        return self


class RankedProxy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    kind: Literal["proxy_node"] = "proxy_node"
    status: Literal["ranked", "filtered", "pending"]
    rank: int = 0
    score: float = 0.0
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    pending: list[str] = Field(default_factory=list)
    filters_failed: list[str] = Field(default_factory=list)
    notes: dict[str, Any] = Field(default_factory=dict)
    scoring_version: str
    rule_snapshot: dict[str, Any] = Field(default_factory=dict)
    runner_id: str
    probe_status: str
    probe_mode: str = ProbeMode.SIMULATED.value
    observed_at: datetime | None = None
    sample_count: int = 0
    availability_rate: float | None = None
    latency_ms: float | None = None
    speed_mb_s: float | None = None
    speed_unit: str = "MB/s"
    loss_pct: float | None = None
    risk: float | None = None
    country_code: str | None = None
    protocol: str = ""
    source_ids: list[str] = Field(default_factory=list)
    remarks: str = ""

    @field_validator("sample_count")
    @classmethod
    def _counter_non_negative(cls, value: int) -> int:
        return _check_counter("counter", value)

    @model_validator(mode="after")
    def _ranked_invariants(self) -> "RankedProxy":
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score must be between 0 and 1")
        if self.rank < 0:
            raise ValueError("rank must not be negative")
        if self.status == "ranked" and self.rank < 1:
            raise ValueError("ranked items require rank >= 1")
        if self.status != "ranked" and self.rank != 0:
            raise ValueError("non-ranked items must have rank 0")
        return self


class RankedEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    kind: Literal["edge_endpoint"] = "edge_endpoint"
    status: Literal["ranked", "filtered", "pending"]
    rank: int = 0
    score: float = 0.0
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    pending: list[str] = Field(default_factory=list)
    filters_failed: list[str] = Field(default_factory=list)
    notes: dict[str, Any] = Field(default_factory=dict)
    scoring_version: str
    rule_snapshot: dict[str, Any] = Field(default_factory=dict)
    runner_id: str
    probe_status: str
    probe_mode: str = ProbeMode.SIMULATED.value
    observed_at: datetime | None = None
    sample_count: int = 0
    availability_rate: float | None = None
    latency_ms: float | None = None
    speed_mb_s: float | None = None
    speed_unit: str = "MB/s"
    loss_pct: float | None = None
    risk: float | None = None
    country_code: str | None = None
    address: str
    port: int
    target_host: str = ""
    tls: bool = False
    host_compatible: bool | None = None
    source_ids: list[str] = Field(default_factory=list)
    remarks: str = ""

    @field_validator("sample_count")
    @classmethod
    def _counter_non_negative(cls, value: int) -> int:
        return _check_counter("counter", value)

    @model_validator(mode="after")
    def _ranked_invariants(self) -> "RankedEndpoint":
        _check_port(self.port)
        if not self.address.strip():
            raise ValueError("address must not be empty")
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score must be between 0 and 1")
        if self.rank < 0:
            raise ValueError("rank must not be negative")
        if self.status == "ranked" and self.rank < 1:
            raise ValueError("ranked items require rank >= 1")
        if self.status != "ranked" and self.rank != 0:
            raise ValueError("non-ranked items must have rank 0")
        return self


class ScoreReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    runner_id: str
    profile: str
    generated_at: datetime = Field(default_factory=_utc_now)
    scoring_version: str
    rule_snapshot: dict[str, Any] = Field(default_factory=dict)
    proxies: list[RankedProxy] = Field(default_factory=list)
    endpoints: list[RankedEndpoint] = Field(default_factory=list)
    issues: list[ScoreIssue] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _counts_non_negative(self) -> "ScoreReport":
        for name, value in self.counts.items():
            _check_counter(name, value)
        return self


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    errors: list[str] = Field(default_factory=list)
    files_checked: list[str] = Field(default_factory=list)


class ExportedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    sha256: str
    size_bytes: int
    entry_count: int = 0

    @field_validator("size_bytes", "entry_count")
    @classmethod
    def _counter_non_negative(cls, value: int) -> int:
        return _check_counter("counter", value)

    @field_validator("sha256")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        text = value.strip().lower()
        if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
            raise ValueError("sha256 must be a lowercase 64-char hex digest")
        return text


class ExportOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "failed"]
    directory: str = ""
    files: list[ExportedFile] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    validation: ValidationReport | None = None
    publishable: bool = False
    counts: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _counts_non_negative(self) -> "ExportOutcome":
        for name, value in self.counts.items():
            _check_counter(name, value)
        return self


class PublishResult(BaseModel):
    """Outcome of publishing the public candidate directory.

    ``visibility`` is always ``public`` because this result describes the
    shared ``output/latest`` directory; the private export directory is
    tracked separately through the export stage view. ``public_urls`` lists
    the remote destinations returned by an enabled upload backend.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "skipped", "blocked", "failed"]
    reason: str = ""
    path: str = ""
    visibility: Literal["public"] = "public"
    files: list[str] = Field(default_factory=list)
    blocked: list[str] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)
    excluded_reasons: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    replaced_previous: bool = False
    cf_candidates_user_supplied: bool = False
    cf_candidates_authorized: bool = False
    public_urls: list[str] = Field(default_factory=list)


def assert_probe_real(result: Any) -> None:
    """Reject results that were produced by a stand-in backend."""
    from nodebench.core.errors import ProbeError

    if isinstance(result, Mapping):
        mode = result.get("probe_mode")
    else:
        mode = getattr(result, "probe_mode", None)
    value = getattr(mode, "value", mode)
    if str(value) != ProbeMode.REAL.value:
        raise ProbeError(
            code="probe_error",
            message=(
                "probe result is not a real measurement: "
                f"probe_mode={mode!r}"
            ),
            retryable=False,
        )


__all__ = [
    "SCHEMA_VERSION",
    "FINGERPRINT_VERSION",
    "RUN_ID_PATTERN",
    "USER_SUPPLIED_LICENSE_TAG",
    "USER_SUPPLIED_LICENSE_TAGS",
    "PRIVATE_VISIBILITY",
    "PUBLIC_VISIBILITY",
    "Status",
    "Kind",
    "ProtocolSupport",
    "ErrorInfo",
    "RunContext",
    "RawItem",
    "ParseIssue",
    "ParsedProxy",
    "ParsedEndpoint",
    "ProxyNode",
    "EdgeEndpoint",
    "SourceReport",
    "ProbeStatus",
    "FailureStage",
    "ProbeMode",
    "PROBE_ERROR_CODES",
    "REASON_DISABLED",
    "REASON_MISSING_BINARY",
    "REASON_MISSING_SPEEDTEST_URL",
    "REASON_MISSING_TARGET_HOST",
    "REASON_BUDGET_EXHAUSTED",
    "REASON_LIMIT_EXCEEDED",
    "REASON_NOT_RUN",
    "REASON_DRY_RUN",
    "REASON_NO_CANDIDATES",
    "SKIP_REASONS",
    "CAPABILITY_SKIP_REASONS",
    "HEADER_FULL",
    "HEADER_TWO",
    "ProbeResultBase",
    "ProxyProbeResult",
    "EndpointProbeResult",
    "assert_probe_real",
    "SCORE_STATUSES",
    "HistorySummary",
    "SourceQuality",
    "PersistSummary",
    "ScoreIssue",
    "UNKNOWN_PLACEHOLDER",
    "ExitObservation",
    "ReputationObservation",
    "IntelligenceReport",
    "RankedProxy",
    "RankedEndpoint",
    "ScoreReport",
    "ValidationReport",
    "ExportedFile",
    "ExportOutcome",
    "PublishResult",
]
