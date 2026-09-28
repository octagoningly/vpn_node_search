from __future__ import annotations

import http.client
import json
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import yaml

from nodebench.core.config import AppConfig
from nodebench.core.schema import FailureStage, ProbeMode, ProbeStatus, ProxyProbeResult
from nodebench.probes.base import (
    BYTES_PER_MB,
    REASON_MISSING_BINARY,
    REASON_MISSING_SPEEDTEST_URL,
    DownloadStats,
    ProbeBudget,
    ProbeFailure,
    ProbeTimeout,
    ProxyTarget,
    TunnelStats,
    classify_exception,
    failure_stage,
    find_free_port,
    resolve_ms,
)

BACKEND_NAME = "mihomo"
PROXY_NAME = "node"


def _first(*values: Any) -> Any:
    for value in values:
        if value is None or value == "" or value == {} or value == []:
            continue
        return value
    return None


def to_mihomo_proxy(target: ProxyTarget) -> dict[str, Any]:
    params = dict(target.params or {})
    credentials = dict(target.credentials or {})
    protocol = (target.protocol or "").strip().lower() or "socks5"
    transport = (target.transport or "tcp").strip().lower() or "tcp"
    security = (target.security or "none").strip().lower()
    proxy: dict[str, Any] = {
        "name": PROXY_NAME,
        "type": protocol,
        "server": target.server,
        "port": int(target.port),
    }
    uuid = _first(credentials.get("uuid"), params.get("uuid"))
    if uuid is not None:
        proxy["uuid"] = str(uuid)
    password = _first(credentials.get("password"), params.get("password"))
    if password is not None:
        proxy["password"] = str(password)
    cipher = _first(params.get("cipher"), credentials.get("cipher"))
    if cipher is not None:
        proxy["cipher"] = str(cipher)
    alter_id = _first(
        params.get("alterId"), params.get("alter-id"), params.get("alter_id")
    )
    if alter_id is not None:
        proxy["alterId"] = int(alter_id)
    flow = _first(params.get("flow"))
    if flow is not None:
        proxy["flow"] = str(flow)
    if transport != "tcp":
        proxy["network"] = transport
    if transport in {"ws", "http", "h2"}:
        ws_opts: dict[str, Any] = {}
        path = _first(params.get("path"), params.get("ws_path"), params.get("service-name"))
        host = _first(params.get("host"), params.get("ws_host"), params.get("authority"))
        if path is not None:
            ws_opts["path"] = str(path)
        if host is not None:
            ws_opts["headers"] = {"Host": str(host)}
        if ws_opts:
            proxy["ws-opts"] = ws_opts
    elif transport == "grpc":
        service_name = _first(
            params.get("service-name"),
            params.get("serviceName"),
            params.get("grpc-service-name"),
        )
        if service_name is not None:
            proxy["grpc-opts"] = {"grpc-service-name": str(service_name)}
    if security in {"tls", "reality"}:
        proxy["tls"] = True
        servername = _first(
            params.get("servername"), params.get("sni"), params.get("peer")
        )
        if servername is not None:
            proxy["servername"] = str(servername)
        fingerprint = _first(params.get("fingerprint"), params.get("client-fingerprint"))
        if fingerprint is not None:
            proxy["client-fingerprint"] = str(fingerprint)
        if security == "reality":
            reality: dict[str, Any] = {}
            public_key = _first(
                params.get("reality_public_key"),
                params.get("public-key"),
                params.get("publicKey"),
                params.get("reality-public-key"),
            )
            short_id = _first(
                params.get("reality_short_id"),
                params.get("short-id"),
                params.get("shortId"),
                params.get("reality-short-id"),
            )
            if public_key is not None:
                reality["public-key"] = str(public_key)
            if short_id is not None:
                reality["short-id"] = str(short_id)
            if reality:
                proxy["reality-opts"] = reality
    else:
        proxy["tls"] = False
    return proxy


def build_mihomo_config(target: ProxyTarget, *, mixed_port: int, api_port: int) -> dict[str, Any]:
    return {
        "mixed-port": int(mixed_port),
        "bind-address": "127.0.0.1",
        "allow-lan": False,
        "mode": "global",
        "log-level": "silent",
        "ipv6": False,
        "external-controller": f"127.0.0.1:{int(api_port)}",
        "secret": "",
        "proxies": [to_mihomo_proxy(target)],
    }


class MihomoController(Protocol):
    def wait_ready(self, timeout: float) -> bool: ...

    def version(self) -> str: ...

    def close(self) -> None: ...


class HttpController:
    """Thin client for the mihomo RESTful controller."""

    def __init__(self, base_url: str, secret: str = "", timeout: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.timeout = float(timeout)

    def _get(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(f"{self.base_url}{path}")
        if self.secret:
            request.add_header("Authorization", f"Bearer {self.secret}")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = response.read()
        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))

    def ready(self) -> bool:
        try:
            self._get("/version")
        except (urllib.error.URLError, OSError, ValueError):
            return False
        return True

    def wait_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if self.ready():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def version(self) -> str:
        payload = self._get("/version")
        return str(payload.get("version", "") or "")

    def close(self) -> None:
        return None


def default_process_factory(command: Sequence[str]) -> Any:
    return subprocess.Popen(
        list(command),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def connect_through_proxy(
    proxy_host: str,
    proxy_port: int,
    host: str,
    port: int,
    *,
    want_tls: bool = True,
    timeout: float = 5.0,
    servername: str | None = None,
) -> TunnelStats:
    """Open an HTTP CONNECT tunnel and optionally hand shake TLS through it."""
    start = time.monotonic()
    connection = http.client.HTTPConnection(proxy_host, proxy_port, timeout=timeout)
    try:
        connection.set_tunnel(host, port)
        connection.connect()
        tcp_ms = (time.monotonic() - start) * 1000.0
        if not want_tls:
            return TunnelStats(tcp_ms=tcp_ms, tls_ms=None)
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        tls_start = time.monotonic()
        with context.wrap_socket(
            connection.sock, server_hostname=servername or host
        ):
            tls_ms = (time.monotonic() - tls_start) * 1000.0
        return TunnelStats(tcp_ms=tcp_ms, tls_ms=tls_ms)
    finally:
        connection.close()


def http_download(
    proxy_host: str,
    proxy_port: int,
    url: str,
    *,
    max_bytes: int,
    timeout: float,
) -> DownloadStats:
    """Pull at most ``max_bytes`` through the local mixed port."""
    proxy = f"http://{proxy_host}:{proxy_port}"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )
    request = urllib.request.Request(
        url, headers={"User-Agent": "nodebench/probe", "Accept": "*/*"}
    )
    started = time.monotonic()
    with opener.open(request, timeout=timeout) as response:
        status = int(getattr(response, "status", None) or response.getcode() or 0)
        received = 0
        first_byte_ms: float | None = None
        while received < max_bytes:
            chunk = response.read(min(65536, max_bytes - received))
            if not chunk:
                break
            if first_byte_ms is None:
                first_byte_ms = (time.monotonic() - started) * 1000.0
            received += len(chunk)
    total_ms = (time.monotonic() - started) * 1000.0
    return DownloadStats(
        download_bytes=received,
        ttfb_ms=first_byte_ms,
        total_ms=total_ms,
        http_status=status,
    )


def entry_connect_ms(
    host: str, port: int, *, timeout: float, want_tls: bool, servername: str = ""
) -> tuple[float, float | None]:
    start = time.monotonic()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        tcp_ms = (time.monotonic() - start) * 1000.0
        if not want_tls:
            return tcp_ms, None
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        tls_start = time.monotonic()
        with context.wrap_socket(sock, server_hostname=servername or host):
            tls_ms = (time.monotonic() - tls_start) * 1000.0
    return tcp_ms, tls_ms


class MihomoProber:
    """One mihomo process per node, measuring the node from this machine."""

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
        process_factory: Callable[[Sequence[str]], Any] = default_process_factory,
        controller_factory: Callable[[str, str, float], MihomoController] | None = None,
        tunnel_probe: Callable[..., TunnelStats] = connect_through_proxy,
        downloader: Callable[..., DownloadStats] = http_download,
        resolver: Callable[[str, float], float] | None = None,
        port_factory: Callable[[], int] = find_free_port,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.budget = budget
        self.run_id = run_id
        self.runner_id = runner_id
        self.run_dir = Path(run_dir)
        self.binary = binary
        self.root = root
        self.process_factory = process_factory
        self.controller_factory = controller_factory
        self.tunnel_probe = tunnel_probe
        self.downloader = downloader
        self.resolver = resolver
        self.port_factory = port_factory
        self.logger = logger
        self._version_cache: str | None = None
        self._version_lock = threading.Lock()

    @property
    def probe_config(self) -> Any:
        return self.config.probe.proxy

    @property
    def speedtest_url(self) -> str:
        return str(self.probe_config.speedtest_url or "").strip()

    @property
    def per_node_timeout(self) -> float:
        return max(1.0, float(self.probe_config.per_node_timeout_s))

    @property
    def api_timeout(self) -> float:
        return max(0.5, float(self.probe_config.api_timeout_s))

    def resolve(self, host: str, timeout: float) -> float:
        if self.resolver is not None:
            return float(self.resolver(host, timeout))
        return resolve_ms(host, timeout)

    def backend_version(self) -> str:
        if not self.binary:
            return ""
        with self._version_lock:
            if self._version_cache is not None:
                return self._version_cache
            version = ""
            try:
                completed = subprocess.run(
                    [self.binary, "-v"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                line = (completed.stdout or completed.stderr or "").strip().splitlines()
                if line:
                    version = line[0].strip()[:80]
            except (OSError, subprocess.SubprocessError):
                version = ""
            self._version_cache = version
            return version

    def skip(self, target: ProxyTarget, reason: str) -> ProxyProbeResult:
        return self._make(
            target,
            status=ProbeStatus.SKIPPED,
            skipped_reason=reason,
            notes={"proxy_protocol": target.protocol, "proxy_transport": target.transport},
        )

    def failure(self, target: ProxyTarget, exc: BaseException) -> ProxyProbeResult:
        info = classify_exception(exc)
        return self._make(
            target,
            status=info.status,
            failure_stage=info.stage,
            attempts=max(1, info.timeouts),
            timeouts=info.timeouts,
            error_code=info.error_code,
            error_message=info.error_message,
            notes={"proxy_protocol": target.protocol, "proxy_transport": target.transport},
        )

    def probe(self, target: ProxyTarget) -> ProxyProbeResult:
        if not self.binary:
            return self.skip(target, REASON_MISSING_BINARY)
        version = self.backend_version()
        if not self.speedtest_url:
            return self._entry_only(target, version)
        return self._full_probe(target, version)

    def _entry_only(self, target: ProxyTarget, version: str) -> ProxyProbeResult:
        """Measure the entry path, then park the node as pending."""
        deadline = time.monotonic() + self.per_node_timeout
        notes: dict[str, Any] = {
            "proxy_protocol": target.protocol,
            "proxy_transport": target.transport,
            "dns_scope": "server",
            "tcp_scope": "entry_direct",
            "pending_reason": "missing_speedtest_url",
        }
        try:
            dns_ms = self.resolve(target.server, self._remaining(deadline))
            tcp_ms, tls_ms = entry_connect_ms(
                target.server,
                target.port,
                timeout=self._remaining(deadline),
                want_tls=target.security == "tls",
                servername=str(
                    _first(
                        (target.params or {}).get("servername"),
                        (target.params or {}).get("sni"),
                        target.server,
                    )
                    or target.server
                ),
            )
        except ProbeTimeout as err:
            return self._make(
                target,
                status=ProbeStatus.TIMEOUT,
                failure_stage=failure_stage(err.stage_name),
                attempts=1,
                timeouts=1,
                error_code="probe_error",
                error_message=err.message,
                backend_version=version,
                notes=notes,
            )
        except ProbeFailure as err:
            return self._make(
                target,
                status=err.status,
                failure_stage=FailureStage(err.stage_name),
                attempts=max(1, err.timeouts),
                timeouts=err.timeouts,
                error_code=err.code,
                error_message=err.message,
                backend_version=version,
                notes=notes,
            )
        except TimeoutError as err:
            return self._make(
                target,
                status=ProbeStatus.TIMEOUT,
                failure_stage=FailureStage.TCP,
                attempts=1,
                timeouts=1,
                error_code="probe_error",
                error_message=f"entry timing timed out: {err}",
                backend_version=version,
                notes=notes,
            )
        except OSError as err:
            stage = FailureStage.DNS if isinstance(err, socket.gaierror) else FailureStage.TCP
            return self._make(
                target,
                status=ProbeStatus.FAIL,
                failure_stage=stage,
                attempts=1,
                error_code="probe_error",
                error_message=f"entry timing failed: {type(err).__name__}: {err}",
                backend_version=version,
                notes=notes,
            )
        notes["server"] = target.server
        return self._make(
            target,
            status=ProbeStatus.SKIPPED,
            skipped_reason=REASON_MISSING_SPEEDTEST_URL,
            backend_version=version,
            dns_ms=dns_ms,
            tcp_ms=tcp_ms,
            tls_ms=tls_ms,
            notes=notes,
        )

    def _full_probe(self, target: ProxyTarget, version: str) -> ProxyProbeResult:
        notes: dict[str, Any] = {
            "proxy_protocol": target.protocol,
            "proxy_transport": target.transport,
            "dns_scope": "speedtest_host",
            "tcp_scope": "tunnel_local_proxy",
        }
        parsed = urllib.parse.urlsplit(self.speedtest_url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        notes["probe_url_scheme"] = parsed.scheme
        if not host:
            return self._make(
                target,
                status=ProbeStatus.SKIPPED,
                skipped_reason=REASON_MISSING_SPEEDTEST_URL,
                backend_version=version,
                notes=notes,
            )
        mixed_port = self.port_factory()
        api_port = self.port_factory()
        config_path = self._write_config(target, mixed_port, api_port)
        process: Any = None
        controller: Any = None
        try:
            process = self._spawn(config_path)
            controller = self._controller(api_port)
            self._wait_ready(process, controller, time.monotonic() + self.api_timeout)
            version = self._controller_version(controller) or version
            deadline = time.monotonic() + self.per_node_timeout
            notes["mixed_port"] = mixed_port
            dns_ms = self.resolve(host, self._remaining(deadline))
            stats = self._tunnel(host, port, mixed_port, deadline)
            download_bytes, ttfb_ms, total_ms, speed_mb_s = self._download(
                mixed_port, deadline, notes
            )
            speed_mbps = None
            if speed_mb_s is not None:
                speed_mbps = speed_mb_s * 8.0
                notes["speed_formula"] = "download_bytes/1000000/total_s"
            return self._make(
                target,
                status=ProbeStatus.OK,
                attempts=1,
                backend_version=version,
                probe_url_host=host,
                measured_via=f"127.0.0.1:{mixed_port}",
                dns_ms=dns_ms,
                tcp_ms=stats.tcp_ms,
                tls_ms=stats.tls_ms,
                ttfb_ms=ttfb_ms,
                total_latency_ms=total_ms,
                download_bytes=download_bytes,
                speed_mbps=speed_mbps,
                speed_mb_s=speed_mb_s,
                notes=notes,
            )
        except ProbeFailure as err:
            notes.setdefault("mixed_port", mixed_port)
            notes.setdefault("measured_via", f"127.0.0.1:{mixed_port}")
            return self._make(
                target,
                status=err.status,
                failure_stage=FailureStage(err.stage_name),
                attempts=max(1, err.timeouts),
                timeouts=err.timeouts,
                error_code=err.code,
                error_message=err.message,
                backend_version=version,
                probe_url_host=host,
                measured_via=f"127.0.0.1:{mixed_port}",
                notes=notes,
            )
        except ProbeTimeout as err:
            notes.setdefault("mixed_port", mixed_port)
            return self._make(
                target,
                status=ProbeStatus.TIMEOUT,
                failure_stage=failure_stage(err.stage_name),
                attempts=1,
                timeouts=1,
                error_code="probe_error",
                error_message=err.message,
                backend_version=version,
                probe_url_host=host,
                measured_via=f"127.0.0.1:{mixed_port}",
                notes=notes,
            )
        except OSError as err:
            notes.setdefault("mixed_port", mixed_port)
            return self._make(
                target,
                status=ProbeStatus.FAIL,
                failure_stage=FailureStage.UNKNOWN,
                attempts=1,
                error_code="probe_error",
                error_message=f"{type(err).__name__}: {err}",
                backend_version=version,
                probe_url_host=host,
                measured_via=f"127.0.0.1:{mixed_port}",
                notes=notes,
            )
        finally:
            self._teardown(controller, process, config_path)

    def _tunnel(self, host: str, port: int, mixed_port: int, deadline: float) -> TunnelStats:
        timeout = self._remaining(deadline)
        try:
            return self.tunnel_probe(
                "127.0.0.1",
                mixed_port,
                host,
                port,
                want_tls=True,
                timeout=timeout,
                servername=host,
            )
        except ProbeFailure:
            raise
        except ProbeTimeout as err:
            raise ProbeFailure(
                FailureStage.TCP.value,
                ProbeStatus.TIMEOUT,
                str(err),
                timeouts=1,
            ) from err
        except (socket.timeout, TimeoutError) as err:
            raise ProbeFailure(
                FailureStage.TCP.value,
                ProbeStatus.TIMEOUT,
                f"tunnel handshake timed out: {err}",
                timeouts=1,
            ) from err
        except ssl.SSLError as err:
            raise ProbeFailure(
                FailureStage.TLS.value,
                ProbeStatus.FAIL,
                f"tls handshake failed: {err}",
            ) from err
        except ConnectionRefusedError as err:
            raise ProbeFailure(
                FailureStage.TCP.value,
                ProbeStatus.FAIL,
                f"tunnel connect refused: {err}",
            ) from err
        except OSError as err:
            raise ProbeFailure(
                FailureStage.TCP.value,
                ProbeStatus.FAIL,
                f"tunnel failed: {type(err).__name__}: {err}",
            ) from err

    def _download(
        self,
        mixed_port: int,
        deadline: float,
        notes: dict[str, Any],
    ) -> tuple[int, float | None, float | None, float | None]:
        if not self.budget.try_consume_download_node():
            notes["download_skipped"] = "max_download_nodes"
            return 0, None, None, None
        allowed = self.budget.try_consume_download(self.budget.per_node_bytes)
        if allowed <= 0:
            notes["download_skipped"] = "total_bytes_limit"
            return 0, None, None, None
        timeout = self._remaining(deadline)
        try:
            stats = self.downloader(
                "127.0.0.1",
                mixed_port,
                self.speedtest_url,
                max_bytes=allowed,
                timeout=timeout,
            )
        except (socket.timeout, TimeoutError) as err:
            raise ProbeFailure(
                FailureStage.DOWNLOAD.value,
                ProbeStatus.TIMEOUT,
                f"download timed out: {err}",
                timeouts=1,
            ) from err
        except urllib.error.HTTPError as err:
            raise ProbeFailure(
                FailureStage.HTTP.value,
                ProbeStatus.FAIL,
                f"download returned http {err.code}",
            ) from err
        except OSError as err:
            raise ProbeFailure(
                FailureStage.DOWNLOAD.value,
                ProbeStatus.FAIL,
                f"download failed: {type(err).__name__}: {err}",
            ) from err
        if stats.total_ms and stats.total_ms > 0 and stats.download_bytes > 0:
            speed_mb_s = stats.download_bytes / BYTES_PER_MB / (stats.total_ms / 1000.0)
        else:
            speed_mb_s = None
        if stats.download_bytes <= 0:
            raise ProbeFailure(
                FailureStage.DOWNLOAD.value,
                ProbeStatus.MEASUREMENT_ERROR,
                "download completed without receiving any payload",
            )
        notes["download_cap_bytes"] = allowed
        notes["http_status"] = stats.http_status
        return stats.download_bytes, stats.ttfb_ms, stats.total_ms, speed_mb_s

    def _remaining(self, deadline: float) -> float:
        return max(0.05, deadline - time.monotonic())

    def _write_config(self, target: ProxyTarget, mixed_port: int, api_port: int) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / f"mihomo-{mixed_port}.yaml"
        payload = build_mihomo_config(target, mixed_port=mixed_port, api_port=api_port)
        try:
            path.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        except OSError as err:
            raise ProbeFailure(
                FailureStage.CONFIG.value,
                ProbeStatus.FAIL,
                f"cannot write mihomo config: {err}",
            ) from err
        return path

    def _spawn(self, config_path: Path) -> Any:
        command = [str(self.binary), "-f", str(config_path), "-d", str(config_path.parent)]
        try:
            return self.process_factory(command)
        except OSError as err:
            raise ProbeFailure(
                FailureStage.PROCESS.value,
                ProbeStatus.FAIL,
                f"cannot start mihomo: {err}",
            ) from err

    def _controller(self, api_port: int) -> MihomoController:
        if self.controller_factory is not None:
            return self.controller_factory(
                f"http://127.0.0.1:{api_port}", "", self.api_timeout
            )
        return HttpController(f"http://127.0.0.1:{api_port}", "", self.api_timeout)

    @staticmethod
    def _controller_version(controller: MihomoController) -> str:
        try:
            return str(controller.version() or "")
        except Exception:
            return ""

    def _wait_ready(
        self, process: Any, controller: MihomoController, deadline: float
    ) -> None:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeFailure(
                    FailureStage.PROCESS.value,
                    ProbeStatus.TIMEOUT,
                    "mihomo controller was not ready within api_timeout_s",
                    timeouts=1,
                )
            poll = getattr(process, "poll", None)
            if callable(poll) and poll() is not None:
                raise ProbeFailure(
                    FailureStage.PROCESS.value,
                    ProbeStatus.FAIL,
                    f"mihomo exited with code {getattr(process, 'returncode', '?')}",
                )
            if controller.wait_ready(min(0.5, remaining)):
                return

    def _teardown(self, controller: Any, process: Any, config_path: Path) -> None:
        if controller is not None:
            try:
                controller.close()
            except Exception:
                pass
        if process is not None:
            for name, timeout in (("terminate", 2), ("kill", 2)):
                method = getattr(process, name, None)
                if not callable(method):
                    continue
                try:
                    method()
                    wait = getattr(process, "wait", None)
                    if callable(wait):
                        wait(timeout=timeout)
                    break
                except Exception:
                    continue
        try:
            config_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _make(self, target: ProxyTarget, *, status: ProbeStatus, **kwargs: Any) -> ProxyProbeResult:
        notes = kwargs.pop("notes", None)
        failure_stage = kwargs.pop("failure_stage", FailureStage.UNKNOWN)
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
        return ProxyProbeResult(
            run_id=self.run_id,
            runner_id=self.runner_id,
            status=status,
            failure_stage=failure_stage,
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
            **kwargs,
        )


__all__ = [
    "BACKEND_NAME",
    "HttpController",
    "MihomoProber",
    "build_mihomo_config",
    "connect_through_proxy",
    "default_process_factory",
    "entry_connect_ms",
    "http_download",
    "to_mihomo_proxy",
]
