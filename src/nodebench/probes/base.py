from __future__ import annotations

import os
import shutil
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from nodebench.core.config import AppConfig
from nodebench.core.context import redact
from nodebench.core.errors import ProbeError
from nodebench.core.schema import (
    CAPABILITY_SKIP_REASONS,
    PROBE_ERROR_CODES,
    REASON_BUDGET_EXHAUSTED,
    REASON_DISABLED,
    REASON_DRY_RUN,
    REASON_LIMIT_EXCEEDED,
    REASON_MISSING_BINARY,
    REASON_MISSING_SPEEDTEST_URL,
    REASON_MISSING_TARGET_HOST,
    REASON_NOT_RUN,
    REASON_NO_CANDIDATES,
    SKIP_REASONS,
    FailureStage,
    ProbeStatus,
    ProxyProbeResult,
)

BYTES_PER_MB = 1_000_000
DEFAULT_DOWNLOAD_MB = 5.0
MIHOMO_BINARY_NAMES = ("mihomo", "clash")
CFST_BINARY_NAMES = ("cloudflareSpeedTest", "CloudflareSpeedTest", "cfst")


class ProbeTimeout(ProbeError):
    """Timeout carrying the stage where the budget was exceeded."""

    def __init__(self, message: str, *, stage: str = "unknown") -> None:
        super().__init__(code="probe_error", message=message, retryable=True)
        self.stage_name = stage


class ProbeFailure(Exception):
    """Internal failure carrying the exact stage that broke."""

    def __init__(
        self,
        stage: str,
        status: ProbeStatus,
        message: str,
        *,
        code: str = "probe_error",
        timeouts: int = 0,
    ) -> None:
        super().__init__(message)
        self.stage_name = stage
        self.status = status
        self.code = error_code_for(code)
        self.message = message
        self.timeouts = int(timeouts)


@dataclass(frozen=True)
class ProxyTarget:
    item_id: str
    server: str
    port: int
    protocol: str = "vless"
    transport: str = "tcp"
    security: str = "tls"
    remarks: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    credentials: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EndpointTarget:
    item_id: str
    address: str
    port: int
    target_host: str = ""
    tls: bool = False
    remarks: str = ""


@dataclass
class TunnelStats:
    tcp_ms: float
    tls_ms: float | None = None
    connected: bool = True


@dataclass
class DownloadStats:
    download_bytes: int
    ttfb_ms: float | None = None
    total_ms: float | None = None
    http_status: int | None = None
    exit_ip: str = ""


@dataclass(frozen=True)
class FailureInfo:
    status: ProbeStatus
    stage: FailureStage
    error_code: str
    error_message: str
    timeouts: int = 0


@dataclass
class ProbeBudget:
    concurrency: int = 10
    max_nodes: int = 100
    max_download_mb_each: float = DEFAULT_DOWNLOAD_MB
    max_download_nodes: int = 20
    total_bytes_limit: int = 0
    total_timeout_s: float = 300.0
    started_at: float = field(default_factory=time.monotonic)
    consumed_nodes: int = 0
    consumed_bytes: int = 0
    consumed_download_nodes: int = 0

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        if self.total_bytes_limit <= 0:
            self.total_bytes_limit = int(
                max(1, self.max_nodes) * max(self.max_download_mb_each, 0.0) * BYTES_PER_MB
            )

    @property
    def deadline(self) -> float:
        return self.started_at + max(0.0, float(self.total_timeout_s))

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    @property
    def remaining_time(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    @property
    def per_node_bytes(self) -> int:
        return int(max(self.max_download_mb_each, 0.0) * BYTES_PER_MB)

    def try_consume(self, n: int = 1) -> bool:
        with self._lock:
            if self.expired:
                return False
            if self.consumed_nodes + n > self.max_nodes:
                return False
            self.consumed_nodes += n
            return True

    def try_consume_download_node(self) -> bool:
        with self._lock:
            if self.consumed_download_nodes >= self.max_download_nodes:
                return False
            self.consumed_download_nodes += 1
            return True

    def try_consume_download(self, requested: int) -> int:
        with self._lock:
            want = max(0, min(int(requested), self.per_node_bytes))
            room = max(0, int(self.total_bytes_limit) - self.consumed_bytes)
            allowed = min(want, room)
            self.consumed_bytes += allowed
            return allowed

    def skip_reason(self) -> str:
        if self.expired:
            return REASON_BUDGET_EXHAUSTED
        return REASON_LIMIT_EXCEEDED


def proxy_budget(config: AppConfig) -> ProbeBudget:
    probe = config.probe.proxy
    override = config.budget.get("probe_download_mb")
    if override:
        total_bytes = int(float(override) * BYTES_PER_MB)
    else:
        total_bytes = int(
            max(1, probe.max_nodes) * max(probe.max_download_mb_each, 0.0) * BYTES_PER_MB
        )
    return ProbeBudget(
        concurrency=max(1, int(probe.concurrency)),
        max_nodes=max(1, int(probe.max_nodes)),
        max_download_mb_each=float(probe.max_download_mb_each),
        max_download_nodes=max(1, int(probe.max_nodes)),
        total_bytes_limit=total_bytes,
        total_timeout_s=float(probe.total_deadline_s),
    )


def cf_budget(config: AppConfig) -> ProbeBudget:
    probe = config.probe.cf
    override = config.budget.get("probe_download_mb")
    if override:
        total_bytes = int(float(override) * BYTES_PER_MB)
    else:
        total_bytes = int(
            max(1, probe.max_download_nodes) * DEFAULT_DOWNLOAD_MB * BYTES_PER_MB
        )
    return ProbeBudget(
        concurrency=max(1, int(probe.concurrency)),
        max_nodes=max(1, int(probe.max_nodes)),
        max_download_mb_each=DEFAULT_DOWNLOAD_MB,
        max_download_nodes=max(1, int(probe.max_download_nodes)),
        total_bytes_limit=total_bytes,
        total_timeout_s=float(probe.total_deadline_s),
    )


class ProbeBackend(Protocol):
    def probe(self, target: Any) -> Any: ...

    def skip(self, target: Any, reason: str) -> Any: ...

    def failure(self, target: Any, exc: BaseException) -> Any: ...


def proxy_target(node: Any) -> ProxyTarget:
    return ProxyTarget(
        item_id=str(node.item_id),
        server=str(node.server),
        port=int(node.port),
        protocol=str(node.protocol),
        transport=str(node.transport),
        security=str(node.security),
        remarks=str(node.remarks),
        params=dict(node.params or {}),
        credentials=dict(node.secrets or {}),
    )


def endpoint_target(edge: Any) -> EndpointTarget:
    return EndpointTarget(
        item_id=str(edge.item_id),
        address=str(edge.address),
        port=int(edge.port),
        target_host=str(edge.target_host or ""),
        tls=bool(edge.tls),
        remarks=str(edge.remarks),
    )


def project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "config" / "default.yaml").is_file():
            return candidate
    return Path(__file__).resolve().parents[3]


def resolve_binary(
    configured_path: str, names: Sequence[str], root: Path | None = None
) -> str | None:
    configured = str(configured_path or "").strip()
    if configured:
        path = Path(os.path.expandvars(configured)).expanduser()
        if not path.is_absolute():
            path = (root or project_root()) / path
        return str(path) if path.is_file() else None
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def find_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def resolve_ms(host: str, timeout: float) -> float:
    started = time.monotonic()

    def _lookup() -> list:
        return socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_lookup)
        try:
            future.result(timeout=max(timeout, 0.001))
        except FutureTimeoutError as err:
            raise ProbeTimeout(
                f"dns lookup for {host} exceeded {timeout:.1f}s", stage="dns"
            ) from err
    return (time.monotonic() - started) * 1000.0


def error_code_for(code: str | None) -> str:
    return code if code in PROBE_ERROR_CODES else "probe_error"


def failure_stage(name: Any) -> FailureStage:
    value = getattr(name, "value", name)
    try:
        return FailureStage(str(value))
    except ValueError:
        return FailureStage.UNKNOWN


def classify_exception(exc: BaseException) -> FailureInfo:
    if isinstance(exc, ProbeFailure):
        return FailureInfo(
            status=exc.status,
            stage=failure_stage(exc.stage_name),
            error_code=exc.code,
            error_message=redact(exc.message),
            timeouts=exc.timeouts,
        )
    if isinstance(exc, ProbeTimeout):
        return FailureInfo(
            status=ProbeStatus.TIMEOUT,
            stage=failure_stage(exc.stage_name),
            error_code="probe_error",
            error_message=redact(str(exc)),
            timeouts=1,
        )
    if isinstance(exc, TimeoutError):
        return FailureInfo(
            status=ProbeStatus.TIMEOUT,
            stage=FailureStage.UNKNOWN,
            error_code="probe_error",
            error_message=redact(f"{type(exc).__name__}: {exc}"),
            timeouts=1,
        )
    if isinstance(exc, ProbeError):
        return FailureInfo(
            status=ProbeStatus.FAIL,
            stage=failure_stage(exc.stage),
            error_code=error_code_for(exc.code),
            error_message=redact(exc.message),
            timeouts=0,
        )
    message = redact(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, socket.gaierror):
        return FailureInfo(
            ProbeStatus.FAIL, FailureStage.DNS, "probe_error", message, 0
        )
    if isinstance(exc, ConnectionError):
        return FailureInfo(
            ProbeStatus.FAIL, FailureStage.TCP, "probe_error", message, 0
        )
    if isinstance(exc, OSError):
        return FailureInfo(
            ProbeStatus.FAIL, FailureStage.TCP, "probe_error", message, 0
        )
    return FailureInfo(
        ProbeStatus.FAIL, FailureStage.UNKNOWN, "probe_error", message, 0
    )


def run_active(
    pairs: Sequence[tuple[int, Any]],
    probe_one: Callable[[Any], Any],
    make_failure: Callable[[Any, BaseException], Any],
    budget: ProbeBudget,
) -> dict[int, Any]:
    results: dict[int, Any] = {}
    if not pairs:
        return results
    targets = {index: target for index, target in pairs}
    workers = max(1, min(int(budget.concurrency), len(pairs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(probe_one, target): index for index, target in pairs
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as err:
                results[index] = make_failure(targets[index], err)
    return results


def run_proxy_batch(
    targets: Sequence[ProxyTarget], backend: ProbeBackend, budget: ProbeBudget
) -> list[ProxyProbeResult]:
    selections: dict[int, ProxyProbeResult] = {}
    active: list[tuple[int, ProxyTarget]] = []
    for index, target in enumerate(targets):
        if budget.try_consume(1):
            active.append((index, target))
        else:
            selections[index] = backend.skip(target, budget.skip_reason())
    selections.update(
        run_active(active, backend.probe, backend.failure, budget)
    )
    return [selections[index] for index in range(len(targets))]


__all__ = [
    "BYTES_PER_MB",
    "CFST_BINARY_NAMES",
    "CAPABILITY_SKIP_REASONS",
    "DEFAULT_DOWNLOAD_MB",
    "DownloadStats",
    "EndpointTarget",
    "FailureInfo",
    "MIHOMO_BINARY_NAMES",
    "ProbeBackend",
    "ProbeBudget",
    "ProbeFailure",
    "ProbeTimeout",
    "ProxyTarget",
    "REASON_BUDGET_EXHAUSTED",
    "REASON_DISABLED",
    "REASON_DRY_RUN",
    "REASON_LIMIT_EXCEEDED",
    "REASON_MISSING_BINARY",
    "REASON_MISSING_SPEEDTEST_URL",
    "REASON_MISSING_TARGET_HOST",
    "REASON_NOT_RUN",
    "REASON_NO_CANDIDATES",
    "SKIP_REASONS",
    "TunnelStats",
    "cf_budget",
    "classify_exception",
    "endpoint_target",
    "error_code_for",
    "failure_stage",
    "find_free_port",
    "project_root",
    "proxy_budget",
    "proxy_target",
    "resolve_binary",
    "resolve_ms",
    "run_active",
    "run_proxy_batch",
]
