from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nodebench.core.config import AppConfig, load_config
from nodebench.core.context import build_run_context
from nodebench.core.errors import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_PROBE_OR_STORAGE,
    EXIT_SOURCES,
    ProbeError,
)
from nodebench.pipeline.orchestrator import _probe_summary, resolve_run_exit, run_pipeline
from nodebench.core.schema import (
    PROBE_ERROR_CODES,
    EndpointProbeResult,
    FailureStage,
    ProbeMode,
    ProbeStatus,
    ProxyProbeResult,
    assert_probe_real,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_PATH = CONFIG_DIR / "default.yaml"
RUN_ID = "20260101T000000Z-abcdef"
RUNNER_ID = "123e4567-e89b-12d3-a456-426614174000"
ITEM_ID = f"hmac-sha256:{'0123456789abcdef' * 4}"

URI_LINE = (
    "vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.10:443"
    "?type=tcp&security=tls&host=node.example.test#sample-192-0-2-10\n"
)

PROBE_NODE_KEYS = {
    "mode",
    "backend",
    "backend_version",
    "skipped_reason",
    "attempted",
    "ok",
    "failed",
    "timeout",
    "measurement_error",
    "incompatible",
    "usable_real",
    "skipped",
    "simulated_results",
}

CRITICAL_NODE = {
    "mode": "real",
    "attempted": 1,
    "ok": 0,
    "usable_real": 0,
    "failed": 1,
    "timeout": 0,
}
ALL_OK_NODE = {
    "mode": "real",
    "attempted": 2,
    "ok": 2,
    "usable_real": 2,
    "failed": 0,
    "timeout": 0,
}
MEASUREMENT_NODE = {
    "mode": "real",
    "attempted": 1,
    "ok": 0,
    "usable_real": 0,
    "failed": 0,
    "timeout": 0,
    "measurement_error": 1,
}
INCOMPATIBLE_NODE = {
    "mode": "real",
    "attempted": 1,
    "ok": 0,
    "usable_real": 0,
    "failed": 0,
    "timeout": 0,
    "incompatible": 1,
}
SKIPPED_MISSING_NODE = {
    "mode": "skip",
    "skipped_reason": "missing_binary",
    "attempted": 0,
    "ok": 0,
    "failed": 0,
    "timeout": 0,
}


def proxy_result(**overrides) -> ProxyProbeResult:
    values = {
        "run_id": RUN_ID,
        "runner_id": RUNNER_ID,
        "status": ProbeStatus.OK,
        "probe_mode": ProbeMode.REAL,
        "backend": "mihomo",
        "item_id": ITEM_ID,
    }
    values.update(overrides)
    return ProxyProbeResult(**values)


def endpoint_result(**overrides) -> EndpointProbeResult:
    values = {
        "run_id": RUN_ID,
        "runner_id": RUNNER_ID,
        "status": ProbeStatus.OK,
        "probe_mode": ProbeMode.REAL,
        "backend": "cfst",
        "item_id": ITEM_ID,
        "address": "192.0.2.1",
        "port": 443,
    }
    values.update(overrides)
    return EndpointProbeResult(**values)


def test_error_codes_are_exactly_the_documented_set():
    assert PROBE_ERROR_CODES == {
        "config_error",
        "source_error",
        "parse_error",
        "probe_error",
        "storage_error",
        "export_error",
        "invalid_row",
    }


def test_ok_result_requires_unknown_failure_stage():
    with pytest.raises(ValidationError):
        proxy_result(status=ProbeStatus.OK, failure_stage=FailureStage.TCP)
    result = proxy_result(status=ProbeStatus.OK, failure_stage=FailureStage.UNKNOWN)
    assert result.status is ProbeStatus.OK


def test_timeouts_cannot_exceed_attempts():
    with pytest.raises(ValidationError):
        proxy_result(attempts=1, timeouts=2)
    proxy_result(attempts=2, timeouts=2)


def test_skipped_result_requires_reason():
    with pytest.raises(ValidationError):
        proxy_result(status=ProbeStatus.SKIPPED)
    result = proxy_result(status=ProbeStatus.SKIPPED, skipped_reason="missing_binary")
    assert result.skipped_reason == "missing_binary"


def test_unknown_error_code_rejected():
    with pytest.raises(ValidationError):
        proxy_result(error_code="made_up")
    result = proxy_result(error_code="config_error")
    assert result.error_code == "config_error"


def test_probe_mode_rejects_other_values():
    with pytest.raises(ValidationError):
        proxy_result(probe_mode="stand-in")
    proxy_result(probe_mode=ProbeMode.SIMULATED)


def test_backend_must_not_be_empty():
    with pytest.raises(ValidationError):
        proxy_result(backend="   ")


def test_result_forbids_extra_fields():
    with pytest.raises(ValidationError):
        proxy_result(unexpected_field=1)


def test_speed_requires_download_bytes():
    with pytest.raises(ValidationError):
        proxy_result(speed_mb_s=2.0, download_bytes=0)
    result = proxy_result(speed_mb_s=2.0, download_bytes=1_000_000)
    assert result.download_bytes == 1_000_000
    assert result.speed_mbps is None


def test_negative_counters_rejected():
    with pytest.raises(ValidationError):
        proxy_result(attempts=-1)
    with pytest.raises(ValidationError):
        proxy_result(timeouts=-1)
    with pytest.raises(ValidationError):
        proxy_result(download_bytes=-5)


def test_endpoint_result_validates_port_and_address():
    endpoint_result()
    with pytest.raises(ValidationError):
        endpoint_result(port=0)
    with pytest.raises(ValidationError):
        endpoint_result(port=65536)
    with pytest.raises(ValidationError):
        endpoint_result(address="   ")
    with pytest.raises(ValidationError):
        endpoint_result(recv_bytes=-1)
    with pytest.raises(ValidationError):
        endpoint_result(sent_bytes=-1)


def test_assert_probe_real_rejects_simulated():
    assert_probe_real(proxy_result(probe_mode=ProbeMode.REAL))
    assert_probe_real({"probe_mode": ProbeMode.REAL})
    with pytest.raises(ProbeError):
        assert_probe_real(proxy_result(probe_mode=ProbeMode.SIMULATED))
    with pytest.raises(ProbeError):
        assert_probe_real({"probe_mode": "simulated"})
    with pytest.raises(ProbeError):
        assert_probe_real({})


def test_resolve_run_exit_priority():
    assert resolve_run_exit() == EXIT_OK
    assert resolve_run_exit(cf_enabled=True, target_host="") == EXIT_CONFIG
    assert (
        resolve_run_exit(cf_enabled=True, target_host="edge.example.test") == EXIT_OK
    )
    assert resolve_run_exit(cf_enabled=True, target_host="   ") == EXIT_CONFIG
    assert resolve_run_exit(run_status="failed") == EXIT_SOURCES


def test_resolve_run_exit_config_error_wins():
    assert resolve_run_exit(probe_results=[{"error_code": "config_error"}]) == (
        EXIT_CONFIG
    )
    failing = proxy_result(
        status=ProbeStatus.FAIL,
        failure_stage=FailureStage.CONFIG,
        error_code="config_error",
    )
    assert resolve_run_exit(run_status="failed", probe_results=[failing]) == EXIT_CONFIG
    assert resolve_run_exit(probe_results=[{"error_code": ""}]) == EXIT_OK


def test_resolve_run_exit_real_probe_failure_exits_four():
    assert resolve_run_exit(probe={"proxy": CRITICAL_NODE}) == EXIT_PROBE_OR_STORAGE
    assert resolve_run_exit(probe={"cf": CRITICAL_NODE}) == EXIT_PROBE_OR_STORAGE
    assert resolve_run_exit(probe={"proxy": ALL_OK_NODE}) == EXIT_OK
    assert resolve_run_exit(probe={"proxy": MEASUREMENT_NODE}) == EXIT_OK
    assert resolve_run_exit(probe={"proxy": INCOMPATIBLE_NODE}) == EXIT_OK
    assert (
        resolve_run_exit(run_status="failed", probe={"proxy": CRITICAL_NODE})
        == EXIT_PROBE_OR_STORAGE
    )


def test_resolve_run_exit_strict_gate():
    assert (
        resolve_run_exit(strict=True, probe={"proxy": SKIPPED_MISSING_NODE})
        == EXIT_PROBE_OR_STORAGE
    )
    assert resolve_run_exit(probe={"proxy": SKIPPED_MISSING_NODE}) == (
        EXIT_PROBE_OR_STORAGE
    )
    for reason in ("disabled", "dry_run", "not_run"):
        node = {"mode": "skip", "skipped_reason": reason}
        assert resolve_run_exit(strict=True, probe={"proxy": node}) == EXIT_OK
    assert resolve_run_exit(strict=True, probe={"proxy": ALL_OK_NODE}) == EXIT_OK


def test_probe_summary_counts_real_results():
    results = [
        proxy_result(status=ProbeStatus.OK, backend_version="mihomo v1.19.0"),
        proxy_result(
            item_id="id-2",
            status=ProbeStatus.FAIL,
            failure_stage=FailureStage.TCP,
            error_code="probe_error",
            attempts=1,
        ),
        proxy_result(
            item_id="id-3",
            status=ProbeStatus.TIMEOUT,
            failure_stage=FailureStage.TCP,
            attempts=1,
            timeouts=1,
            error_code="probe_error",
        ),
        proxy_result(
            item_id="id-4",
            status=ProbeStatus.SKIPPED,
            skipped_reason="probe_limit_exceeded",
        ),
    ]
    node = _probe_summary(results, "mihomo")
    assert set(node) == PROBE_NODE_KEYS
    assert node == {
        "mode": "real",
        "backend": "mihomo",
        "backend_version": "mihomo v1.19.0",
        "skipped_reason": "",
        "attempted": 3,
        "ok": 1,
        "failed": 1,
        "timeout": 1,
        "measurement_error": 0,
        "incompatible": 0,
        "usable_real": 1,
        "skipped": 1,
        "simulated_results": 0,
    }


def test_probe_summary_empty_and_all_skipped():
    empty = _probe_summary([], "mihomo")
    assert set(empty) == PROBE_NODE_KEYS
    assert empty["mode"] == "skip"
    assert empty["skipped_reason"] == "no_candidates"
    assert empty["attempted"] == 0
    assert empty["backend"] == "mihomo"
    results = [
        proxy_result(status=ProbeStatus.SKIPPED, skipped_reason="missing_binary"),
        proxy_result(
            item_id="id-2",
            status=ProbeStatus.SKIPPED,
            skipped_reason="missing_binary",
        ),
    ]
    node = _probe_summary(results, "mihomo")
    assert node["mode"] == "skip"
    assert node["skipped_reason"] == "missing_binary"
    assert node["skipped"] == 2
    assert node["attempted"] == 0


def test_probe_summary_simulated_mode():
    results = [
        proxy_result(
            status=ProbeStatus.FAIL,
            probe_mode=ProbeMode.SIMULATED,
            failure_stage=FailureStage.TCP,
            attempts=1,
        )
    ]
    node = _probe_summary(results, "mihomo")
    assert node["mode"] == "simulated"
    assert node["simulated_results"] == 1
    assert node["attempted"] == 1


def pipeline_config(tmp_path: Path) -> AppConfig:
    config = load_config(DEFAULT_PATH, None, env={})
    source = tmp_path / "nodes.txt"
    source.write_text(URI_LINE, encoding="utf-8")
    config.sources.local.paths = [str(source)]
    config.output_dir = str(tmp_path / "out")
    return config


def run(config: AppConfig, **kwargs) -> tuple[dict, list]:
    ctx = build_run_context(config, {"profile": config.profile})
    sink: list = kwargs.pop("probe_sink", None) or []
    result = run_pipeline(config, ctx, probe_sink=sink, **kwargs)
    return result, sink


def test_pipeline_collect_only_reports_not_run(tmp_path: Path):
    config = pipeline_config(tmp_path)
    result, sink = run(config, run_probes=False)
    assert set(result["probe"]) == {"proxy", "cf"}
    assert set(result["probe"]["proxy"]) == PROBE_NODE_KEYS
    assert result["probe"]["proxy"]["mode"] == "skip"
    assert result["probe"]["proxy"]["skipped_reason"] == "not_run"
    assert result["probe"]["cf"]["backend"] == "cfst"
    assert result["probe"]["cf"]["skipped_reason"] == "not_run"
    assert result["status"] == "ok"
    assert sink == []
    assert all(
        preview["probe_status"] == "not_probed"
        for preview in result["items_preview"]["proxy_nodes"]
    )
    assert result["stages_pending"] == [
        "inspect",
        "persist",
        "score",
        "export",
        "publish",
    ]


def test_pipeline_dry_run_reports_dry_run(tmp_path: Path):
    config = pipeline_config(tmp_path)
    result, sink = run(config, dry_run=True)
    assert result["dry_run"] is True
    assert result["probe"]["proxy"]["skipped_reason"] == "dry_run"
    assert result["probe"]["cf"]["skipped_reason"] == "dry_run"
    assert result["status"] == "ok"
    assert sink == []
    assert all(
        preview["probe_status"] == "not_probed"
        for preview in result["items_preview"]["proxy_nodes"]
    )


def test_pipeline_wet_run_missing_binary_degrades(tmp_path: Path):
    config = pipeline_config(tmp_path)
    config.probe.proxy.mihomo_path = str(tmp_path / "absent-mihomo")
    result, sink = run(config, dry_run=False)
    node = result["probe"]["proxy"]
    assert result["status"] == "partial"
    assert node["mode"] == "skip"
    assert node["skipped_reason"] == "missing_binary"
    assert node["skipped"] == 1
    assert result["probe"]["cf"]["skipped_reason"] == "disabled"
    assert len(sink) == 1
    assert sink[0].status is ProbeStatus.SKIPPED
    assert sink[0].skipped_reason == "missing_binary"
    assert sink[0].probe_mode is ProbeMode.NOT_RUN
    assert sink[0].attempts == 0
    assert all(
        preview["probe_status"] == "skipped"
        for preview in result["items_preview"]["proxy_nodes"]
    )
    strict_code = resolve_run_exit(
        run_status=str(result["status"]), probe=result["probe"], strict=True
    )
    assert strict_code == EXIT_PROBE_OR_STORAGE
    lenient_code = resolve_run_exit(
        run_status=str(result["status"]), probe=result["probe"]
    )
    assert lenient_code == EXIT_PROBE_OR_STORAGE


def test_resolve_run_exit_critical_skip_reasons():
    for reason in ("missing_binary", "missing_target_host"):
        node = {"mode": "skip", "skipped_reason": reason}
        assert resolve_run_exit(probe={"proxy": node}) == EXIT_PROBE_OR_STORAGE
        assert (
            resolve_run_exit(strict=True, probe={"cf": node})
            == EXIT_PROBE_OR_STORAGE
        )
        assert (
            resolve_run_exit(run_status="failed", probe={"cf": node})
            == EXIT_PROBE_OR_STORAGE
        )


def test_resolve_run_exit_non_critical_skip_reasons():
    for reason in (
        "disabled",
        "dry_run",
        "not_run",
        "no_candidates",
        "missing_speedtest_url",
        "budget_exhausted",
        "probe_limit_exceeded",
    ):
        node = {"mode": "skip", "skipped_reason": reason}
        assert resolve_run_exit(probe={"proxy": node}) == EXIT_OK
    for reason in ("disabled", "dry_run", "not_run"):
        node = {"mode": "skip", "skipped_reason": reason}
        assert resolve_run_exit(strict=True, probe={"proxy": node}) == EXIT_OK
    for reason in (
        "missing_speedtest_url",
        "budget_exhausted",
        "probe_limit_exceeded",
        "no_candidates",
    ):
        node = {"mode": "skip", "skipped_reason": reason}
        assert (
            resolve_run_exit(strict=True, probe={"proxy": node})
            == EXIT_PROBE_OR_STORAGE
        )


def test_collect_only_pipeline_exits_zero(tmp_path: Path):
    config = pipeline_config(tmp_path)
    result, sink = run(config, run_probes=False)
    assert sink == []
    assert result["probe"]["proxy"]["skipped_reason"] == "not_run"
    code = resolve_run_exit(run_status=str(result["status"]), probe=result["probe"])
    assert code == EXIT_OK
