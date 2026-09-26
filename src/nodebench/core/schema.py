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
    content_type: Literal["uri_list", "base64_sub", "yaml", "csv", "text"]
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
    "Status",
    "Kind",
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
    "ProbeResultBase",
    "ProxyProbeResult",
    "EndpointProbeResult",
    "assert_probe_real",
]
