from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from nodebench.core.config import AppConfig, load_config
from nodebench.core.errors import ProbeError
from nodebench.core.schema import FailureStage, ProbeStatus
from nodebench.probes import (
    REASON_DISABLED,
    REASON_LIMIT_EXCEEDED,
    REASON_MISSING_BINARY,
    REASON_MISSING_TARGET_HOST,
    EndpointTarget,
    ProbeBudget,
    cf_budget,
    default_endpoint_check,
    make_cfst_prober,
    parse_cfst_csv,
    run_cf_batch,
)
from nodebench.probes.base import ProbeTimeout
from nodebench.probes.cfst import (
    CHECK_FAIL,
    CHECK_OK,
    CHECK_TIMEOUT,
    EndpointCheck,
    family_mismatch,
    normalize_ip,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = PROJECT_ROOT / "config" / "default.yaml"
RUN_ID = "20260101T000000Z-abcdef"
RUNNER_ID = "123e4567-e89b-12d3-a456-426614174000"
ITEM_ID = f"hmac-sha256:{'0123456789abcdef' * 4}"
ITEM_OTHER = f"hmac-sha256:{'c' * 64}"

CSV_HEADER = "IP 地址,已发送,已接收,丢包率,平均延迟,下载速度(MB/s),地区码"
CSV_TEXT = (
    f"{CSV_HEADER}\n"
    "192.0.2.1,4,4,0.0,23.5,28.64,HKG\n"
    "198.51.100.2,4,4,0.0,45.1,,\n"
)


def sample_target(**overrides) -> EndpointTarget:
    values = {
        "item_id": ITEM_ID,
        "address": "192.0.2.1",
        "port": 443,
        "target_host": "edge.example.test",
        "tls": True,
        "remarks": "sample",
    }
    values.update(overrides)
    return EndpointTarget(**values)


def make_prober(
    tmp_path: Path,
    *,
    binary: str | None = None,
    target_host: str = "edge.example.test",
    **kwargs,
):
    config: AppConfig = load_config(DEFAULT_PATH, None, env={})
    config.probe.cf.enabled = True
    config.probe.cf.target_host = target_host
    config.probe.cf.cfst_path = str(tmp_path / "absent-cfst")
    config.probe.cf.speedtest_url = "https://speed.cloudflare.com/__down"
    return make_cfst_prober(
        config,
        cf_budget(config),
        run_id=RUN_ID,
        runner_id=RUNNER_ID,
        run_dir=tmp_path / "run",
        binary=binary,
        **kwargs,
    )


def passing_check(target, **kwargs) -> EndpointCheck:
    check = EndpointCheck()
    check.tcp_ok = True
    check.tls_ok = True
    check.https_ok = True
    check.host_compatible = True
    check.tcp_ms = 6.5
    check.tls_ms = 40.0
    check.http_status = 200
    return check


def test_parse_cfst_csv_reads_valid_rows():
    rows = parse_cfst_csv(CSV_TEXT)
    assert set(rows) == {"192.0.2.1", "198.51.100.2"}
    row = rows["192.0.2.1"]
    assert row.sent == 4
    assert row.received == 4
    assert row.loss_pct == 0.0
    assert row.latency_ms == 23.5
    assert row.speed_mb_s == 28.64
    assert row.region == "HKG"
    sparse = rows["198.51.100.2"]
    assert sparse.speed_mb_s is None
    assert sparse.region == ""
    assert sparse.fields["ip"] == "198.51.100.2"


def test_parse_cfst_csv_accepts_bom_header():
    rows = parse_cfst_csv("\ufeff" + CSV_TEXT)
    assert "192.0.2.1" in rows


def test_parse_cfst_csv_rejects_bad_tables():
    with pytest.raises(ProbeError) as empty:
        parse_cfst_csv("")
    assert empty.value.code == "invalid_row"
    assert parse_cfst_csv(f"{CSV_HEADER}\n") == {}
    with pytest.raises(ProbeError) as short_row:
        parse_cfst_csv(f"{CSV_HEADER}\n192.0.2.1,4,4\n")
    assert short_row.value.code == "invalid_row"
    with pytest.raises(ProbeError) as bad_latency:
        parse_cfst_csv(f"{CSV_HEADER}\n192.0.2.1,4,4,0.0,fast,28.64,HKG\n")
    assert bad_latency.value.code == "invalid_row"
    with pytest.raises(ProbeError) as host_row:
        parse_cfst_csv(f"{CSV_HEADER}\nhostname.example,4,4,0.0,1.0,2.0,\n")
    assert host_row.value.code == "invalid_row"
    lenient = parse_cfst_csv(f"\n192.0.2.1,4,4,0.0,1.0,2.0,HKG\n")
    assert "192.0.2.1" in lenient


def test_normalize_ip_and_family_rules():
    assert normalize_ip("  [2001:DB8::1]  ") == "2001:db8::1"
    assert normalize_ip("192.0.2.7") == "192.0.2.7"
    assert family_mismatch("192.0.2.1", "4") is False
    assert family_mismatch("2001:db8::1", "4") is True
    assert family_mismatch("2001:db8::1", "6") is False
    assert family_mismatch("192.0.2.1", "all") is False
    assert family_mismatch("edge.example.test", "4") is False


def test_skip_reason_priority(tmp_path: Path):
    no_binary = make_prober(tmp_path)
    assert no_binary.skip_reason(sample_target()) == REASON_MISSING_BINARY
    no_host = make_prober(tmp_path, binary="fake-cfst", target_host="")
    assert no_host.skip_reason(sample_target()) == REASON_MISSING_TARGET_HOST
    ready = make_prober(tmp_path, binary="fake-cfst")
    assert ready.skip_reason(sample_target()) == ""
    assert ready.skip_reason(sample_target(port=8080)) == REASON_DISABLED
    assert ready.skip_reason(sample_target(address="2001:db8::1")) == REASON_DISABLED


def test_probe_without_binary_skips(tmp_path: Path):
    prober = make_prober(tmp_path)
    result = prober.probe(sample_target())
    assert result.status is ProbeStatus.SKIPPED
    assert result.skipped_reason == REASON_MISSING_BINARY
    assert result.backend == "cfst"


def test_probe_ok_without_metrics(tmp_path: Path):
    prober = make_prober(tmp_path, binary="fake-cfst", check=passing_check)
    result = prober.probe(sample_target())
    assert result.status is ProbeStatus.OK
    assert result.failure_stage is FailureStage.UNKNOWN
    assert result.attempts == 1
    assert result.timeouts == 0
    assert result.notes["tcp_check_ms"] == 6.5
    assert result.notes["metrics_missing"] is True
    assert result.latency_ms is None
    assert result.speed_mb_s is None
    assert result.host_compatible is True


def test_probe_merges_metrics_from_csv(tmp_path: Path):
    def csv_runner(command, timeout) -> str:
        return CSV_TEXT

    prober = make_prober(
        tmp_path, binary="fake-cfst", check=passing_check, runner=csv_runner
    )
    prober.collect_metrics([sample_target()])
    result = prober.probe(sample_target())
    assert result.status is ProbeStatus.OK
    assert result.latency_ms == 23.5
    assert result.speed_mb_s == 28.64
    assert result.loss_pct == 0.0
    assert result.region == "HKG"
    assert result.sent_bytes == 4
    assert result.recv_bytes == 4
    assert result.csv_row["ip"] == "192.0.2.1"
    assert "metrics_missing" not in result.notes


def test_metrics_failure_downgrades_ok(tmp_path: Path):
    def bad_runner(command, timeout) -> str:
        raise ProbeError(
            code="probe_error", message="cfst exited with code 1", retryable=True
        )

    prober = make_prober(
        tmp_path, binary="fake-cfst", check=passing_check, runner=bad_runner
    )
    prober.collect_metrics([sample_target()])
    result = prober.probe(sample_target())
    assert result.status is ProbeStatus.MEASUREMENT_ERROR
    assert result.failure_stage is FailureStage.DOWNLOAD
    assert result.error_code == "probe_error"
    assert result.notes["metrics_error"] is True
    assert result.attempts == 1
    assert result.timeouts == 0


def test_metrics_timeout_downgrades_ok(tmp_path: Path):
    def slow_runner(command, timeout) -> str:
        raise ProbeTimeout("cfst exceeded 5.0s", stage="download")

    prober = make_prober(
        tmp_path, binary="fake-cfst", check=passing_check, runner=slow_runner
    )
    prober.collect_metrics([sample_target()])
    result = prober.probe(sample_target())
    assert result.status is ProbeStatus.TIMEOUT
    assert result.failure_stage is FailureStage.DOWNLOAD
    assert result.timeouts == 1


def test_failure_classifies_connection_error(tmp_path: Path):
    prober = make_prober(tmp_path, binary="fake-cfst")
    result = prober.failure(sample_target(), ConnectionRefusedError("refused"))
    assert result.status is ProbeStatus.FAIL
    assert result.failure_stage is FailureStage.TCP
    assert result.error_code == "probe_error"
    assert result.attempts == 1


def test_default_endpoint_check_refused_port_fails():
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    port = int(holder.getsockname()[1])
    holder.close()
    target = sample_target(address="127.0.0.1", port=port, tls=False)
    check = default_endpoint_check(
        target,
        target_host="edge.example.test",
        want_tls=False,
        tcp_timeout=1.0,
    )
    assert check.status in {CHECK_FAIL, CHECK_TIMEOUT}
    assert check.stage == "tcp"
    assert check.tcp_ok is None
    assert check.error_message


def test_default_endpoint_check_local_listener_ok():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(2)
    port = int(server.getsockname()[1])
    connections: list[socket.socket] = []
    stop = threading.Event()

    def accept_loop() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except (socket.timeout, OSError):
                continue
            connections.append(connection)

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    try:
        target = sample_target(address="127.0.0.1", port=port, tls=False)
        check = default_endpoint_check(
            target,
            target_host="edge.example.test",
            want_tls=False,
            tcp_timeout=1.0,
        )
    finally:
        stop.set()
        server.close()
        thread.join(timeout=2)
        for connection in connections:
            connection.close()
    assert check.status == CHECK_OK
    assert check.tcp_ok is True
    assert check.tcp_ms is not None
    assert check.tcp_ms >= 0.0
    assert check.stage == ""


def test_run_cf_batch_skips_and_runs_in_order(tmp_path: Path):
    def csv_runner(command, timeout) -> str:
        return CSV_TEXT

    prober = make_prober(
        tmp_path, binary="fake-cfst", check=passing_check, runner=csv_runner
    )
    budget = ProbeBudget(max_nodes=1)
    targets = [
        sample_target(item_id=ITEM_ID),
        sample_target(item_id=ITEM_OTHER, port=8080),
        sample_target(item_id=f"hmac-sha256:{'d' * 64}"),
    ]
    results = run_cf_batch(targets, prober, budget)
    assert [result.item_id for result in results] == [
        targets[0].item_id,
        targets[1].item_id,
        targets[2].item_id,
    ]
    assert results[0].status is ProbeStatus.OK
    assert results[1].status is ProbeStatus.SKIPPED
    assert results[1].skipped_reason == REASON_DISABLED
    assert results[2].status is ProbeStatus.SKIPPED
    assert results[2].skipped_reason == REASON_LIMIT_EXCEEDED
    assert budget.consumed_nodes == 1
