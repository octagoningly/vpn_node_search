from __future__ import annotations

import ipaddress
import os
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nodebench.core.config import AppConfig
from nodebench.core.context import redact
from nodebench.core.errors import ProbeError
from nodebench.core.schema import (
    EndpointProbeResult,
    FailureStage,
    ProbeMode,
    ProbeStatus,
)
from nodebench.probes.base import (
    CFST_BINARY_NAMES,
    REASON_DISABLED,
    REASON_MISSING_BINARY,
    REASON_MISSING_TARGET_HOST,
    EndpointTarget,
    ProbeBudget,
    ProbeTimeout,
    classify_exception,
    error_code_for,
    failure_stage,
    project_root,
    resolve_binary,
    run_active,
)

BACKEND_NAME = "cfst"
CF_ERROR_CODES = frozenset({522, 523, 524, 525, 526, 527, 530})
CSV_COLUMNS = 7
MAX_LATENCY_THREADS = 200
TLS_PORTS = frozenset({443})

CHECK_OK = ""
CHECK_FAIL = "fail"
CHECK_TIMEOUT = "timeout"
CHECK_INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class CsvRow:
    ip: str
    sent: int | None = None
    received: int | None = None
    loss_pct: float | None = None
    latency_ms: float | None = None
    speed_mb_s: float | None = None
    region: str = ""
    fields: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class EndpointCheck:
    status: str = CHECK_OK
    stage: str = ""
    error_code: str = ""
    error_message: str = ""
    timeouts: int = 0
    tcp_ok: bool | None = None
    tls_ok: bool | None = None
    https_ok: bool | None = None
    host_compatible: bool | None = None
    http_status: int | None = None
    tcp_ms: float | None = None
    tls_ms: float | None = None


def normalize_ip(value: Any) -> str:
    text = str(value or "").strip().strip("[]")
    try:
        return ipaddress.ip_address(text).compressed.lower()
    except ValueError:
        return text.lower()


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(str(value).strip().strip("[]"))
    except ValueError:
        return False
    return True


def _region(value: str) -> str:
    text = str(value or "").strip()
    if text.upper() in {"N/A", "NA", "-"}:
        return ""
    return text


def _int_or_none(value: str, position: int, name: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError as err:
        raise ProbeError(
            code="invalid_row",
            message=f"cfst row {position} has a non-numeric {name}: {text!r}",
            retryable=False,
        ) from err


def _float_or_none(value: str, position: int, name: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as err:
        raise ProbeError(
            code="invalid_row",
            message=f"cfst row {position} has a non-numeric {name}: {text!r}",
            retryable=False,
        ) from err


def _build_row(cells: Sequence[str], position: int) -> CsvRow:
    sent = _int_or_none(cells[1], position, "sent")
    received = _int_or_none(cells[2], position, "received")
    loss_pct = _float_or_none(cells[3], position, "loss_pct")
    latency_ms = _float_or_none(cells[4], position, "latency_ms")
    speed_mb_s = _float_or_none(cells[5], position, "speed_mb_s")
    region = _region(cells[6])
    return CsvRow(
        ip=normalize_ip(cells[0]),
        sent=sent,
        received=received,
        loss_pct=loss_pct,
        latency_ms=latency_ms,
        speed_mb_s=speed_mb_s,
        region=region,
        fields={
            "ip": normalize_ip(cells[0]),
            "sent": sent,
            "received": received,
            "loss_pct": loss_pct,
            "latency_ms": latency_ms,
            "speed_mb_s": speed_mb_s,
            "region": region,
        },
    )


def parse_cfst_csv(text: str) -> dict[str, CsvRow]:
    """Parse a CloudflareSpeedTest result table into rows keyed by ip."""
    raw = "" if text is None else str(text)
    lines = [line for line in raw.lstrip("\ufeff").splitlines() if line.strip()]
    if not lines:
        raise ProbeError(
            code="invalid_row",
            message="cfst produced an empty result table",
            retryable=False,
        )
    rows: dict[str, CsvRow] = {}
    for position, line in enumerate(lines, start=1):
        cells = [cell.strip() for cell in line.split(",")]
        if not cells[0]:
            raise ProbeError(
                code="invalid_row",
                message=f"cfst row {position} has an empty address",
                retryable=False,
            )
        if not _is_ip(cells[0]):
            if position == 1:
                continue
            raise ProbeError(
                code="invalid_row",
                message=f"cfst row {position} has an unexpected address {cells[0]!r}",
                retryable=False,
            )
        if len(cells) < CSV_COLUMNS:
            raise ProbeError(
                code="invalid_row",
                message=(
                    f"cfst row {position} has {len(cells)} of {CSV_COLUMNS} columns"
                ),
                retryable=False,
            )
        row = _build_row(cells, position)
        rows[row.ip] = row
    return rows


def _family_for(ip_family: str) -> int:
    if ip_family == "4":
        return socket.AF_INET
    if ip_family == "6":
        return socket.AF_INET6
    return socket.AF_UNSPEC


def family_mismatch(address: str, ip_family: str) -> bool:
    if ip_family not in {"4", "6"}:
        return False
    try:
        version = ipaddress.ip_address(str(address).strip().strip("[]")).version
    except ValueError:
        return False
    return str(version) != ip_family


def _tcp_connect(
    address: str, port: int, family: int, timeout: float
) -> tuple[socket.socket, float]:
    started = time.monotonic()
    infos = socket.getaddrinfo(address, port, family, socket.SOCK_STREAM)
    if not infos:
        raise socket.gaierror(f"no address found for {address}")
    last_error: OSError | None = None
    for addr_family, sock_type, proto, _canon, sockaddr in infos:
        sock = socket.socket(addr_family, sock_type, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
        except OSError as err:
            sock.close()
            last_error = err
            continue
        return sock, (time.monotonic() - started) * 1000.0
    if last_error is not None:
        raise last_error
    raise OSError(f"unable to connect to {address}:{port}")


def _read_status_line(sock: socket.socket) -> int:
    buffer = b""
    while b"\r\n" not in buffer and len(buffer) <= 65536:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buffer += chunk
    if not buffer:
        raise ConnectionError("empty response from endpoint")
    first = buffer.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = first.split(" ")
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        raise ValueError(f"unexpected status line: {first[:80]!r}")
    try:
        return int(parts[1])
    except ValueError as err:
        raise ValueError(f"unexpected status line: {first[:80]!r}") from err


def _mark(
    check: EndpointCheck,
    *,
    status: str,
    stage: str,
    message: str,
    timeouts: int = 0,
) -> EndpointCheck:
    check.status = status
    check.stage = stage
    check.error_message = message
    check.timeouts = timeouts
    return check


def default_endpoint_check(
    target: EndpointTarget,
    *,
    target_host: str,
    ip_family: str = "4",
    want_tls: bool = True,
    tcp_timeout: float = 5.0,
    tls_timeout: float = 5.0,
    http_timeout: float = 5.0,
) -> EndpointCheck:
    """Probe tcp, tls (with host verification) and http for one endpoint."""
    check = EndpointCheck()
    family = _family_for(ip_family)
    sock: socket.socket | None = None
    try:
        sock, tcp_ms = _tcp_connect(target.address, int(target.port), family, tcp_timeout)
    except TimeoutError:
        return _mark(
            check,
            status=CHECK_TIMEOUT,
            stage=FailureStage.TCP.value,
            message=f"tcp connect to {target.address}:{target.port} timed out",
            timeouts=1,
        )
    except socket.gaierror as err:
        return _mark(
            check,
            status=CHECK_FAIL,
            stage=FailureStage.DNS.value,
            message=f"dns lookup failed for {target.address}: {err}",
        )
    except OSError as err:
        return _mark(
            check,
            status=CHECK_FAIL,
            stage=FailureStage.TCP.value,
            message=f"tcp connect to {target.address}:{target.port} failed: {err}",
        )
    check.tcp_ok = True
    check.tcp_ms = tcp_ms
    if not want_tls:
        return check

    tls_sock: socket.socket | None = None
    try:
        context = ssl.create_default_context()
        sock.settimeout(tls_timeout)
        tls_start = time.monotonic()
        tls_sock = context.wrap_socket(sock, server_hostname=target_host)
        sock = None
    except TimeoutError:
        _close(sock)
        return _mark(
            check,
            status=CHECK_TIMEOUT,
            stage=FailureStage.TLS.value,
            message=f"tls handshake with {target_host} timed out",
            timeouts=1,
        )
    except ssl.SSLCertVerificationError as err:
        _close(sock)
        check.tls_ok = False
        check.host_compatible = False
        return _mark(
            check,
            status=CHECK_INCOMPATIBLE,
            stage=FailureStage.TLS.value,
            message=(
                f"certificate rejected for {target_host}: "
                f"{getattr(err, 'verify_message', None) or err}"
            ),
        )
    except ssl.SSLError as err:
        _close(sock)
        check.tls_ok = False
        check.host_compatible = False
        return _mark(
            check,
            status=CHECK_INCOMPATIBLE,
            stage=FailureStage.TLS.value,
            message=f"tls handshake with {target_host} failed: {err}",
        )
    except OSError as err:
        _close(sock)
        check.tls_ok = False
        check.host_compatible = False
        return _mark(
            check,
            status=CHECK_INCOMPATIBLE,
            stage=FailureStage.TLS.value,
            message=f"tls handshake with {target_host} failed: {err}",
        )
    check.tls_ok = True
    check.host_compatible = True
    check.tls_ms = (time.monotonic() - tls_start) * 1000.0

    status: int | None = None
    try:
        tls_sock.settimeout(http_timeout)
        tls_sock.sendall(_http_request(target_host))
        status = _read_status_line(tls_sock)
    except TimeoutError:
        _close(tls_sock)
        return _mark(
            check,
            status=CHECK_TIMEOUT,
            stage=FailureStage.HTTP.value,
            message=f"http request to {target_host} timed out",
            timeouts=1,
        )
    except (ConnectionError, OSError, ValueError) as err:
        _close(tls_sock)
        return _mark(
            check,
            status=CHECK_FAIL,
            stage=FailureStage.HTTP.value,
            message=f"http request to {target_host} failed: {err}",
        )
    finally:
        _close(tls_sock)

    check.http_status = status
    if status in CF_ERROR_CODES:
        check.https_ok = False
        return _mark(
            check,
            status=CHECK_INCOMPATIBLE,
            stage=FailureStage.HTTP.value,
            message=f"cloudflare returned {status} for {target_host}",
        )
    if status is not None and status >= 500:
        check.https_ok = False
        return _mark(
            check,
            status=CHECK_FAIL,
            stage=FailureStage.HTTP.value,
            message=f"http request to {target_host} returned {status}",
        )
    check.https_ok = True
    return check


def _http_request(target_host: str) -> bytes:
    host = str(target_host or "").strip()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        host = host.encode("ascii", "ignore").decode("ascii")
    lines = (
        "GET / HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "User-Agent: nodebench-probe/1.0\r\n"
        "Accept: */*\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    return lines.encode("ascii")


def _close(sock: socket.socket | None) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def _option_value(command: Sequence[str], flag: str) -> str:
    items = list(command)
    for index, item in enumerate(items[:-1]):
        if item == flag:
            return items[index + 1]
    return ""


_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def build_probe_env(bypass_system_proxy: bool = True) -> dict[str, str]:
    """Environment for CFST: default strips local proxy so speeds stay real."""
    env = os.environ.copy()
    if bypass_system_proxy:
        for key in _PROXY_ENV_KEYS:
            env.pop(key, None)
        env["NO_PROXY"] = "*"
        env["no_proxy"] = "*"
    return env


def default_cfst_runner(
    command: Sequence[str],
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> str:
    """Run CloudflareSpeedTest and return the result table it wrote.

    A non-zero exit or timeout must not throw away a result file CFST
    already flushed — the table is the product, the exit code is not.
    """
    csv_path = _option_value(command, "-o")

    def _csv_text() -> str:
        if not csv_path:
            return ""
        path = Path(csv_path)
        if path.is_file() and path.stat().st_size > 0:
            return path.read_text(encoding="utf-8", errors="replace")
        return ""

    run_env = dict(env) if env is not None else build_probe_env(True)
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=max(1.0, float(timeout)),
            env=run_env,
        )
    except subprocess.TimeoutExpired as err:
        # CFST writes the table as it goes; keep whatever landed on disk.
        partial = _csv_text()
        if partial.strip():
            return partial
        raise ProbeTimeout(
            f"cfst exceeded {float(timeout):.1f}s", stage=FailureStage.DOWNLOAD.value
        ) from err
    except OSError as err:
        raise ProbeError(
            code="probe_error",
            message=f"unable to start cfst: {err}",
            retryable=False,
        ) from err
    stdout = completed.stdout or ""
    table = _csv_text()
    if completed.returncode:
        if table.strip():
            return table
        detail = (completed.stderr or stdout or "").strip()[:400]
        raise ProbeError(
            code="probe_error",
            message=f"cfst exited with code {completed.returncode}: {detail}",
            retryable=True,
        )
    if csv_path:
        # CFST skips writing the result file when no IP passed the filters;
        # banner text on stdout is not a CSV table.
        return table
    return stdout


class CfstProber:
    """CloudflareSpeedTest for latency/speed plus host level compat checks."""

    def __init__(
        self,
        config: AppConfig,
        budget: ProbeBudget,
        *,
        run_id: str,
        runner_id: str,
        run_dir: Path,
        binary: str | None = None,
        root: Path | None = None,
        runner: Callable[[Sequence[str], float], str] = default_cfst_runner,
        check: Callable[..., EndpointCheck] = default_endpoint_check,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.budget = budget
        self.run_id = run_id
        self.runner_id = runner_id
        self.run_dir = Path(run_dir)
        self.root = root
        self.binary = binary
        self.runner = runner
        if runner is default_cfst_runner:
            # Default runner: honour bypass_system_proxy (default on) so
            # Clash/TUN cannot reroute the download being measured.
            probe_env = build_probe_env(
                bool(getattr(config.probe.cf, "bypass_system_proxy", True))
            )
            _base = runner

            def _bound_runner(
                cmd: Sequence[str],
                timeout: float,
                _env: Mapping[str, str] = probe_env,
                _fn=_base,
            ) -> str:
                return _fn(cmd, timeout, env=_env)

            self.runner = _bound_runner
        self.check_factory = check
        self.logger = logger
        self._metrics: dict[tuple[str, int], CsvRow] = {}
        self._metrics_error: BaseException | None = None
        self._version_cache: str | None = None
        self._version_lock = threading.Lock()
        self._resolved_ips: dict[tuple[str, int], str] = {}

    @property
    def probe_config(self) -> Any:
        return self.config.probe.cf

    @property
    def target_host(self) -> str:
        return str(self.probe_config.target_host or "").strip()

    @property
    def speedtest_url(self) -> str:
        return str(self.probe_config.speedtest_url or "").strip()

    @property
    def ip_family(self) -> str:
        return str(self.probe_config.ip_family or "4")

    @property
    def allowed_ports(self) -> frozenset[int]:
        return frozenset(int(port) for port in self.probe_config.allowed_ports)

    @property
    def download_disabled(self) -> bool:
        return not self.speedtest_url

    def backend_version(self) -> str:
        if not self.binary:
            return ""
        with self._version_lock:
            if self._version_cache is not None:
                return self._version_cache
            version = ""
            try:
                completed = subprocess.run(
                    [self.binary, "-h"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdin=subprocess.DEVNULL,
                    timeout=5,
                )
                text = f"{completed.stdout or ''}{completed.stderr or ''}"
                for line in text.splitlines():
                    if "CloudflareSpeedTest" in line:
                        version = line.strip()[:80]
                        break
            except (OSError, subprocess.SubprocessError):
                version = ""
            self._version_cache = version
            return version

    def skip_reason(self, target: EndpointTarget) -> str:
        if not self.binary:
            return REASON_MISSING_BINARY
        if not self.target_host:
            return REASON_MISSING_TARGET_HOST
        if int(target.port) not in self.allowed_ports:
            return REASON_DISABLED
        if family_mismatch(target.address, self.ip_family):
            return REASON_DISABLED
        return ""

    def skip(self, target: EndpointTarget, reason: str) -> EndpointProbeResult:
        return self._make(target, status=ProbeStatus.SKIPPED, skipped_reason=reason)

    def failure(self, target: EndpointTarget, exc: BaseException) -> EndpointProbeResult:
        info = classify_exception(exc)
        return self._make(
            target,
            status=info.status,
            failure_stage=info.stage,
            attempts=1,
            timeouts=info.timeouts,
            error_code=info.error_code,
            error_message=info.error_message,
        )

    def lookup_metric(self, target: EndpointTarget) -> CsvRow | None:
        key = (normalize_ip(target.address), int(target.port))
        if key in self._metrics:
            return self._metrics[key]
        ip = self._resolved_ips.get(
            (str(target.address).strip().lower(), int(target.port))
        )
        if ip:
            return self._metrics.get((ip, int(target.port)))
        return None

    def unmeasured_failure(self, target: EndpointTarget) -> EndpointProbeResult:
        """CFST never reached this candidate -- treat as timeout, not fake ok."""
        return self._make(
            target,
            status=ProbeStatus.TIMEOUT,
            failure_stage="tcp",
            attempts=1,
            timeouts=1,
            error_code="probe_error",
            error_message="not reachable in cfst bulk scan",
            notes={"cfst_unmeasured": True},
        )

    def probe(self, target: EndpointTarget) -> EndpointProbeResult:
        reason = self.skip_reason(target)
        if reason:
            return self.skip(target, reason)
        check = self.check_factory(
            target,
            target_host=self.target_host,
            ip_family=self.ip_family,
            want_tls=int(target.port) in TLS_PORTS or bool(target.tls),
            tcp_timeout=max(0.1, float(self.probe_config.tcp_timeout_s)),
            tls_timeout=max(0.1, float(self.probe_config.tls_timeout_s)),
            http_timeout=max(0.1, float(self.probe_config.http_timeout_s)),
        )
        return self._result(target, check)

    def collect_metrics(self, targets: Sequence[EndpointTarget]) -> None:
        self._metrics = {}
        self._metrics_error = None
        if not targets:
            return
        groups: dict[int, list[EndpointTarget]] = {}
        for target in targets:
            groups.setdefault(int(target.port), []).append(target)
        errors: list[BaseException] = []
        for port, group in sorted(groups.items()):
            try:
                rows = self._run_cfst(group, port)
            except ProbeError as err:
                # One port failing must not discard another port's table.
                errors.append(err)
                continue
            except OSError as err:
                errors.append(
                    ProbeError(
                        code="probe_error",
                        message=f"cfst failed: {err}",
                        retryable=False,
                    )
                )
                continue
            for row in rows:
                self._metrics[(row.ip, port)] = row
        if errors and not self._metrics:
            self._metrics_error = errors[0]

    def _resolve_target_ip(self, target: EndpointTarget) -> str | None:
        """Return a literal IP for CFST input; resolve hostnames once (bounded)."""
        key = (str(target.address).strip().lower(), int(target.port))
        if key in self._resolved_ips:
            return self._resolved_ips[key] or None
        text = str(target.address).strip().strip("[]")
        ip: str | None = None
        if _is_ip(text):
            ip = normalize_ip(text)
        else:
            try:
                infos = socket.getaddrinfo(
                    text, int(target.port), socket.AF_UNSPEC, socket.SOCK_STREAM
                )
                for info in infos:
                    ip = normalize_ip(info[4][0])
                    if ip:
                        break
            except OSError:
                ip = None
        self._resolved_ips[key] = ip or ""
        return ip

    def _run_cfst(
        self, group: Sequence[EndpointTarget], port: int
    ) -> list[CsvRow]:
        if not self.binary:
            raise ProbeError(
                code="config_error",
                message="cfst binary is not available",
                retryable=False,
            )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        resolved: dict[str, EndpointTarget] = {}
        for target in group:
            ip = self._resolve_target_ip(target)
            if ip:
                resolved.setdefault(ip, target)
        if not resolved:
            return []
        ips = sorted(resolved)
        ip_file = self.run_dir / f"cfst-{port}.ips.txt"
        csv_file = self.run_dir / f"cfst-{port}.result.csv"
        ip_file.write_text("\n".join(ips) + "\n", encoding="utf-8")
        # Each download sample may take ~10s; shrink -dn to the time left
        # so one port cannot starve the rest of the deadline.
        remaining = max(1.0, float(self.budget.remaining_time))
        time_budget_nodes = max(1, int(remaining // 12))
        download_nodes = max(
            1, min(int(self.budget.max_download_nodes), len(ips), time_budget_nodes)
        )
        threads = min(MAX_LATENCY_THREADS, max(1, len(ips)))
        command = [
            str(self.binary),
            "-f",
            str(ip_file),
            "-o",
            str(csv_file),
            "-tp",
            str(port),
            "-dn",
            str(download_nodes),
            "-n",
            str(threads),
            "-t",
            "2",
        ]
        if self.speedtest_url:
            command += ["-url", self.speedtest_url]
        else:
            command += ["-dd"]
        timeout = max(1.0, self.budget.remaining_time)
        text = self.runner(command, timeout)
        if not str(text or "").strip():
            return []
        return list(parse_cfst_csv(text).values())

    def _result(
        self, target: EndpointTarget, check: EndpointCheck
    ) -> EndpointProbeResult:
        notes = self._base_notes(target)
        if check.tcp_ms is not None:
            notes["tcp_check_ms"] = round(float(check.tcp_ms), 2)
        if check.tls_ms is not None:
            notes["tls_check_ms"] = round(float(check.tls_ms), 2)
        if check.http_status is not None:
            notes["http_status"] = int(check.http_status)

        status, stage, code, message, attempts, timeouts = self._outcome(check)
        row = self._metrics.get((normalize_ip(target.address), int(target.port)))
        if row is None:
            ip = self._resolved_ips.get(
                (str(target.address).strip().lower(), int(target.port))
            ) or self._resolve_target_ip(target)
            if ip:
                row = self._metrics.get((ip, int(target.port)))
        # Only demote when THIS target has no usable metrics — a failed
        # sibling port must not poison targets CFST already measured.
        if status is ProbeStatus.OK and row is None and self._metrics_error is not None:
            status, stage, code, message, timeouts = self._metrics_outcome()
            notes["metrics_error"] = True
        if status is ProbeStatus.OK and row is None:
            notes["metrics_missing"] = True

        kwargs: dict[str, Any] = {
            "failure_stage": stage,
            "attempts": attempts,
            "timeouts": timeouts,
            "error_code": code,
            "error_message": message,
            "notes": notes,
            "tcp_ok": check.tcp_ok,
            "https_ok": check.https_ok,
            "host_compatible": check.host_compatible,
        }
        if row is not None:
            kwargs.update(
                loss_pct=row.loss_pct,
                latency_ms=row.latency_ms,
                speed_mb_s=None if self.download_disabled else row.speed_mb_s,
                region=row.region,
                sent_bytes=row.sent,
                recv_bytes=row.received,
                csv_row=dict(row.fields),
            )
        return self._make(target, status=status, **kwargs)

    def _outcome(
        self, check: EndpointCheck
    ) -> tuple[ProbeStatus, FailureStage, str, str, int, int]:
        message = redact(check.error_message or "")
        if check.status == CHECK_TIMEOUT:
            stage = failure_stage(check.stage)
            if stage is FailureStage.UNKNOWN:
                stage = FailureStage.TCP
            return (
                ProbeStatus.TIMEOUT,
                stage,
                error_code_for(check.error_code),
                message or "probe timed out",
                1,
                max(1, int(check.timeouts)),
            )
        if check.status == CHECK_FAIL:
            stage = failure_stage(check.stage)
            if stage is FailureStage.UNKNOWN:
                stage = FailureStage.TCP
            return (
                ProbeStatus.FAIL,
                stage,
                error_code_for(check.error_code),
                message,
                1,
                0,
            )
        if check.status == CHECK_INCOMPATIBLE:
            stage = failure_stage(check.stage)
            if stage is FailureStage.UNKNOWN:
                stage = FailureStage.TLS
            return (
                ProbeStatus.INCOMPATIBLE,
                stage,
                error_code_for(check.error_code),
                message,
                1,
                0,
            )
        return ProbeStatus.OK, FailureStage.UNKNOWN, "", "", 1, 0

    def _metrics_outcome(self) -> tuple[ProbeStatus, FailureStage, str, str, int]:
        err = self._metrics_error
        code = error_code_for(getattr(err, "code", "") or "")
        message = redact(getattr(err, "message", None) or str(err))
        if isinstance(err, ProbeTimeout):
            return ProbeStatus.TIMEOUT, FailureStage.DOWNLOAD, code, message, 1
        return ProbeStatus.MEASUREMENT_ERROR, FailureStage.DOWNLOAD, code, message, 0

    def _base_notes(self, target: EndpointTarget) -> dict[str, Any]:
        return {
            "measured_via": f"{target.address}:{int(target.port)}",
            "tcp_scope": "direct",
            "target_host": self.target_host,
        }

    def _make(
        self, target: EndpointTarget, *, status: ProbeStatus, **kwargs: Any
    ) -> EndpointProbeResult:
        notes = kwargs.pop("notes", None)
        failure_stage_value = kwargs.pop("failure_stage", FailureStage.UNKNOWN)
        probe_mode = kwargs.pop("probe_mode", None)
        if probe_mode is None:
            probe_mode = (
                ProbeMode.NOT_RUN
                if status is ProbeStatus.SKIPPED
                else ProbeMode.REAL
            )
        skipped_reason = kwargs.pop("skipped_reason", "")
        attempts = int(kwargs.pop("attempts", 0))
        timeouts = int(kwargs.pop("timeouts", 0))
        error_code = kwargs.pop("error_code", "")
        error_message = kwargs.pop("error_message", "")
        backend_version = kwargs.pop("backend_version", "")
        return EndpointProbeResult(
            run_id=self.run_id,
            runner_id=self.runner_id,
            status=status,
            failure_stage=failure_stage_value,
            probe_mode=probe_mode,
            backend=BACKEND_NAME,
            backend_version=backend_version,
            skipped_reason=skipped_reason,
            attempts=attempts,
            timeouts=timeouts,
            error_code=error_code,
            error_message=error_message,
            notes=dict(notes or {}),
            item_id=target.item_id,
            address=target.address,
            port=int(target.port),
            target_host=self.target_host,
            tls=bool(target.tls),
            **kwargs,
        )


def run_cf_batch(
    targets: Sequence[EndpointTarget], prober: CfstProber, budget: ProbeBudget
) -> list[EndpointProbeResult]:
    selections: dict[int, EndpointProbeResult] = {}
    active: list[tuple[int, EndpointTarget]] = []
    for index, target in enumerate(targets):
        reason = prober.skip_reason(target)
        if reason:
            selections[index] = prober.skip(target, reason)
        elif budget.try_consume(1):
            active.append((index, target))
        else:
            selections[index] = prober.skip(target, budget.skip_reason())
    # Bulk CFST latency/loss first -- cheap scan of every candidate.
    prober.collect_metrics([target for _index, target in active])

    # Only run the slow per-target TCP/TLS/HTTP check on candidates CFST
    # actually measured (or the fastest tops). Everything else is dead weight
    # and would just burn the deadline on timeouts.
    detailed: list[tuple[int, EndpointTarget]] = []
    quick_fail: list[tuple[int, EndpointTarget]] = []
    for index, target in active:
        row = prober.lookup_metric(target)
        if row is not None:
            detailed.append((index, target))
        else:
            quick_fail.append((index, target))

    # Cap detailed checks; prefer lower CFST latency first.
    max_detailed = max(1, int(budget.max_download_nodes) * 4)
    if len(detailed) > max_detailed:
        def _latency_key(item: tuple[int, EndpointTarget]) -> float:
            row = prober.lookup_metric(item[1])
            if row is None or row.latency_ms is None:
                return 1e9
            return float(row.latency_ms)

        detailed.sort(key=_latency_key)
        overflow = detailed[max_detailed:]
        detailed = detailed[:max_detailed]
        quick_fail.extend(overflow)

    selections.update(run_active(detailed, prober.probe, prober.failure, budget))
    for index, target in quick_fail:
        selections[index] = prober.unmeasured_failure(target)
    return [selections[index] for index in range(len(targets))]


def make_cfst_prober(
    config: AppConfig,
    budget: ProbeBudget,
    *,
    run_id: str,
    runner_id: str,
    run_dir: Path,
    root: Path | None = None,
    **kwargs: Any,
) -> CfstProber:
    project = root or project_root()
    binary = kwargs.pop(
        "binary", None
    ) or resolve_binary(config.probe.cf.cfst_path, CFST_BINARY_NAMES, project)
    return CfstProber(
        config,
        budget,
        run_id=run_id,
        runner_id=runner_id,
        run_dir=run_dir,
        binary=binary,
        root=project,
        **kwargs,
    )


__all__ = [
    "BACKEND_NAME",
    "CF_ERROR_CODES",
    "CsvRow",
    "CfstProber",
    "EndpointCheck",
    "default_cfst_runner",
    "default_endpoint_check",
    "family_mismatch",
    "make_cfst_prober",
    "normalize_ip",
    "parse_cfst_csv",
    "run_cf_batch",
]
