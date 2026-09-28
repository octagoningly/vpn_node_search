from __future__ import annotations

import socket
import threading
from pathlib import Path

from nodebench.core.config import AppConfig, load_config
from nodebench.core.schema import FailureStage, ProbeMode, ProbeStatus
from nodebench.probes import (
    REASON_LIMIT_EXCEEDED,
    REASON_MISSING_BINARY,
    REASON_MISSING_SPEEDTEST_URL,
    ProbeBudget,
    ProxyTarget,
    build_mihomo_config,
    proxy_budget,
    run_proxy_batch,
    to_mihomo_proxy,
)
from nodebench.probes.base import DownloadStats, ProbeTimeout, TunnelStats
from nodebench.probes.mihomo import MihomoProber

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = PROJECT_ROOT / "config" / "default.yaml"
RUN_ID = "20260101T000000Z-abcdef"
RUNNER_ID = "123e4567-e89b-12d3-a456-426614174000"
UUID = "123e4567-e89b-12d3-a456-426614174000"
ITEM_A = f"hmac-sha256:{'a' * 64}"
ITEM_B = f"hmac-sha256:{'b' * 64}"


def sample_target(**overrides) -> ProxyTarget:
    values = {
        "item_id": ITEM_A,
        "server": "192.0.2.10",
        "port": 443,
        "protocol": "vless",
        "transport": "tcp",
        "security": "tls",
        "remarks": "sample",
        "params": {
            "uuid": UUID,
            "servername": "edge.example.test",
            "fingerprint": "chrome",
        },
        "credentials": {},
    }
    values.update(overrides)
    return ProxyTarget(**values)


def make_prober(
    tmp_path: Path, *, binary: str | None = None, speedtest_url: str = "", **kwargs
) -> MihomoProber:
    config: AppConfig = load_config(DEFAULT_PATH, None, env={})
    config.probe.proxy.speedtest_url = speedtest_url
    return MihomoProber(
        config,
        proxy_budget(config),
        run_id=RUN_ID,
        runner_id=RUNNER_ID,
        run_dir=tmp_path / "run",
        binary=binary,
        **kwargs,
    )


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None

    def poll(self):
        return None

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


class FakeController:
    def __init__(self, ready: bool = True, version: str = "mihomo v1.19.0") -> None:
        self.ready = ready
        self.version_text = version
        self.closed = False

    def wait_ready(self, timeout: float) -> bool:
        return self.ready

    def version(self) -> str:
        return self.version_text

    def close(self) -> None:
        self.closed = True


def fake_full_probe_kwargs(ports: tuple[int, int]) -> dict:
    port_iterator = iter(ports)
    return {
        "resolver": lambda host, timeout: 0.6,
        "process_factory": lambda command: FakeProcess(),
        "controller_factory": lambda base_url, secret, timeout: FakeController(),
        "tunnel_probe": lambda *args, **kwargs: TunnelStats(tcp_ms=12.5, tls_ms=31.0),
        "downloader": lambda *args, **kwargs: DownloadStats(
            download_bytes=2_000_000,
            ttfb_ms=80.0,
            total_ms=1000.0,
            http_status=200,
        ),
        "port_factory": lambda: next(port_iterator),
    }


def test_to_mihomo_proxy_vless_tls():
    proxy = to_mihomo_proxy(sample_target())
    assert proxy["name"] == "node"
    assert proxy["type"] == "vless"
    assert proxy["server"] == "192.0.2.10"
    assert proxy["port"] == 443
    assert proxy["uuid"] == UUID
    assert proxy["tls"] is True
    assert proxy["servername"] == "edge.example.test"
    assert proxy["client-fingerprint"] == "chrome"
    assert "network" not in proxy
    assert "password" not in proxy


def test_to_mihomo_proxy_trojan_websocket():
    target = sample_target(
        protocol="trojan",
        transport="ws",
        params={
            "password": "sample-password",
            "path": "/ws",
            "host": "edge.example.test",
            "servername": "edge.example.test",
        },
    )
    proxy = to_mihomo_proxy(target)
    assert proxy["type"] == "trojan"
    assert proxy["network"] == "ws"
    assert proxy["password"] == "sample-password"
    assert proxy["ws-opts"] == {
        "path": "/ws",
        "headers": {"Host": "edge.example.test"},
    }
    assert proxy["tls"] is True


def test_to_mihomo_proxy_reality_and_plaintext():
    reality = to_mihomo_proxy(
        sample_target(
            security="reality",
            params={
                "uuid": UUID,
                "sni": "edge.example.test",
                "public-key": "sample-public-key",
                "short-id": "abcd1234",
            },
        )
    )
    assert reality["tls"] is True
    assert reality["servername"] == "edge.example.test"
    assert reality["reality-opts"] == {
        "public-key": "sample-public-key",
        "short-id": "abcd1234",
    }
    plain = to_mihomo_proxy(
        sample_target(protocol="socks5", security="none", port=1080, params={})
    )
    assert plain["type"] == "socks5"
    assert plain["tls"] is False
    assert "uuid" not in plain


def test_to_mihomo_proxy_reads_normalized_reality_params():
    """URI/YAML parsers normalize reality keys; the prober must still emit them."""
    reality = to_mihomo_proxy(
        sample_target(
            security="reality",
            params={
                "uuid": UUID,
                "reality_public_key": "sample-public-key",
                "reality_short_id": "abcd1234",
            },
        )
    )
    assert reality["reality-opts"] == {
        "public-key": "sample-public-key",
        "short-id": "abcd1234",
    }


def test_build_mihomo_config_shape():
    config = build_mihomo_config(sample_target(), mixed_port=18080, api_port=19090)
    assert config["mixed-port"] == 18080
    assert config["external-controller"] == "127.0.0.1:19090"
    assert config["mode"] == "global"
    assert config["bind-address"] == "127.0.0.1"
    assert config["allow-lan"] is False
    assert [proxy["name"] for proxy in config["proxies"]] == ["node"]
    groups = config["proxy-groups"]
    assert groups[0]["name"] == "GLOBAL"
    assert groups[0]["proxies"] == ["node"]


def test_skip_result_is_not_run_mode_with_reason(tmp_path: Path):
    result = make_prober(tmp_path).skip(sample_target(), REASON_MISSING_BINARY)
    assert result.status is ProbeStatus.SKIPPED
    assert result.skipped_reason == REASON_MISSING_BINARY
    assert result.probe_mode is ProbeMode.NOT_RUN
    assert result.attempts == 0
    assert result.failure_stage is FailureStage.UNKNOWN
    assert result.backend == "mihomo"
    assert result.error_code == ""
    assert result.notes["proxy_protocol"] == "vless"
    assert result.notes["proxy_transport"] == "tcp"


def test_probe_without_binary_skips(tmp_path: Path):
    result = make_prober(tmp_path).probe(sample_target())
    assert result.status is ProbeStatus.SKIPPED
    assert result.skipped_reason == REASON_MISSING_BINARY


def test_entry_only_path_measures_then_parks_pending(tmp_path: Path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = int(listener.getsockname()[1])
    stop = threading.Event()

    def accept_loop() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except (socket.timeout, OSError):
                continue
            connection.close()

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    try:
        prober = make_prober(tmp_path, binary="fake-mihomo", resolver=lambda h, t: 0.4)
        target = sample_target(server="127.0.0.1", port=port, security="none")
        result = prober.probe(target)
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=2)
    assert result.status is ProbeStatus.SKIPPED
    assert result.skipped_reason == REASON_MISSING_SPEEDTEST_URL
    assert result.probe_mode is ProbeMode.NOT_RUN
    assert result.dns_ms == 0.4
    assert result.tcp_ms is not None
    assert result.tcp_ms >= 0.0
    assert result.notes["pending_reason"] == REASON_MISSING_SPEEDTEST_URL
    assert result.notes["tcp_scope"] == "entry_direct"
    assert result.notes["server"] == "127.0.0.1"


def test_entry_only_dns_timeout(tmp_path: Path):
    def boom(host: str, timeout: float) -> float:
        raise ProbeTimeout("dns lookup for 192.0.2.10 exceeded 1.0s", stage="dns")

    prober = make_prober(tmp_path, binary="fake-mihomo", resolver=boom)
    result = prober.probe(sample_target())
    assert result.status is ProbeStatus.TIMEOUT
    assert result.failure_stage is FailureStage.DNS
    assert result.attempts == 1
    assert result.timeouts == 1
    assert result.error_code == "probe_error"
    assert result.notes["pending_reason"] == REASON_MISSING_SPEEDTEST_URL


def test_full_probe_ok_with_fakes(tmp_path: Path):
    prober = make_prober(
        tmp_path,
        binary="fake-mihomo",
        speedtest_url="https://speed.cloudflare.com/__down",
        **fake_full_probe_kwargs((18080, 19090)),
    )
    result = prober.probe(sample_target())
    assert result.status is ProbeStatus.OK
    assert result.failure_stage is FailureStage.UNKNOWN
    assert result.attempts == 1
    assert result.backend_version == "mihomo v1.19.0"
    assert result.dns_ms == 0.6
    assert result.tcp_ms == 12.5
    assert result.tls_ms == 31.0
    assert result.ttfb_ms == 80.0
    assert result.total_latency_ms == 1000.0
    assert result.download_bytes == 2_000_000
    assert result.speed_mb_s == 2.0
    assert result.speed_mbps == 16.0
    assert result.probe_url_host == "speed.cloudflare.com"
    assert result.measured_via == "127.0.0.1:18080"
    assert result.notes["http_status"] == 200


def test_full_probe_controller_never_ready_times_out(tmp_path: Path):
    kwargs = fake_full_probe_kwargs((18081, 19091))
    kwargs["controller_factory"] = (
        lambda base_url, secret, timeout: FakeController(ready=False)
    )
    prober = make_prober(
        tmp_path,
        binary="fake-mihomo",
        speedtest_url="https://speed.cloudflare.com/__down",
        **kwargs,
    )
    prober.config.probe.proxy.api_timeout_s = 0.5
    result = prober.probe(sample_target())
    assert result.status is ProbeStatus.TIMEOUT
    assert result.failure_stage is FailureStage.PROCESS
    assert result.attempts == 1
    assert result.timeouts == 1
    assert result.error_code == "probe_error"
    assert result.notes["mixed_port"] == 18081


def test_failure_classifies_gaierror_as_dns(tmp_path: Path):
    prober = make_prober(tmp_path, binary="fake-mihomo")
    result = prober.failure(sample_target(), socket.gaierror("name resolution failed"))
    assert result.status is ProbeStatus.FAIL
    assert result.failure_stage is FailureStage.DNS
    assert result.error_code == "probe_error"
    assert result.attempts == 1
    assert result.timeouts == 0


def test_run_proxy_batch_budget_skips_preserve_order(tmp_path: Path):
    prober = make_prober(tmp_path, binary="fake-mihomo")
    budget = ProbeBudget(max_nodes=0)
    targets = [
        sample_target(item_id=ITEM_A),
        sample_target(item_id=ITEM_B),
    ]
    results = run_proxy_batch(targets, prober, budget)
    assert [result.item_id for result in results] == [ITEM_A, ITEM_B]
    assert all(result.status is ProbeStatus.SKIPPED for result in results)
    assert all(result.skipped_reason == REASON_LIMIT_EXCEEDED for result in results)
    assert budget.consumed_nodes == 0


def test_run_proxy_batch_runs_active_target(tmp_path: Path):
    prober = make_prober(
        tmp_path,
        binary="fake-mihomo",
        speedtest_url="https://speed.cloudflare.com/__down",
        **fake_full_probe_kwargs((18082, 19092)),
    )
    results = run_proxy_batch([sample_target()], prober, proxy_budget(prober.config))
    assert len(results) == 1
    assert results[0].status is ProbeStatus.OK
    assert results[0].speed_mb_s == 2.0
