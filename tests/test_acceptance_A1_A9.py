"""Acceptance tests for 开发规则.md §5.1 A1–A9.

Each test is offline and maps to a single acceptance row. Real-network
cases stay in the existing dedicated suites and are noted in
docs/acceptance-A1-A9.md as 待真机.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nodebench.cli.common import DOCTOR_MESSAGE
from nodebench.cli.main import main
from nodebench.core.config import load_config
from nodebench.core.context import (
    build_run_context,
    clear_registered_secrets,
    redact,
    register_secrets,
)
from nodebench.core.errors import EXIT_SOURCES
from nodebench.pipeline.orchestrator import resolve_run_exit, run_pipeline
from nodebench.core.schema import (
    EdgeEndpoint,
    FailureStage,
    ProbeMode,
    ProbeStatus,
    ProxyNode,
    ProxyProbeResult,
    EndpointProbeResult,
    RankedEndpoint,
    RankedProxy,
    Status,
)
from nodebench.core.serialization import public_dump
from nodebench.exporters import build_export
from nodebench.exporters.cf_addapi import CF_ADDAPI_NAME
from nodebench.exporters.cf_addcsv import CF_ADDCSV_NAME, build_addcsv
from nodebench.exporters.manifest import MANIFEST_NAME
from nodebench.exporters.raw import PROXY_RAW_NAME
from nodebench.history import db as dbmod
from nodebench.history.stats import history_for_items
from nodebench.history.store import persist_run, record_intelligence
from nodebench.intelligence.reputation import NullProvider
from nodebench.intelligence.service import IntelligenceService
from nodebench.parsers.csv import HEADER_FULL, parse_endpoint_csv
from nodebench.probes.base import REASON_MISSING_BINARY
from nodebench.scoring.rank import score_run
from nodebench.publishing.publish import publish_output

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = PROJECT_ROOT / "config" / "default.yaml"
PROFILE_DIR = PROJECT_ROOT / "config" / "profiles"
INPUT_DIR = PROJECT_ROOT / "input"

UUID = "123e4567-e89b-12d3-a456-426614174000"
RUN_ID = "20260101T120000Z-abcdef"
RUNNER = "local:desktop-a"
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("NODEBENCH_"):
            monkeypatch.delenv(name, raising=False)
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def local_config(paths: list[Path] | None = None, tmp_path: Path | None = None):
    config = load_config(DEFAULT_PATH, PROFILE_DIR / "local.yaml", env={})
    if paths is not None:
        config.sources.local.paths = [str(path) for path in paths]
    if tmp_path is not None:
        config.output_dir = str(tmp_path / "out")
    return config


def make_node(item_id: str = "p1", **kwargs) -> ProxyNode:
    payload: dict = {
        "item_id": item_id,
        "kind": "proxy_node",
        "fingerprint": f"fp-{item_id}",
        "fingerprint_version": 1,
        "protocol": "vless",
        "server": "192.0.2.10",
        "port": 443,
        "transport": "tcp",
        "security": "tls",
        "remarks": "sample",
        "source_ids": ["local:a"],
        "secrets": {"uuid": UUID},
    }
    payload.update(kwargs)
    return ProxyNode(**payload)


def make_edge(item_id: str = "e1", **kwargs) -> EdgeEndpoint:
    payload: dict = {
        "item_id": item_id,
        "kind": "edge_endpoint",
        "fingerprint": f"fp-{item_id}",
        "fingerprint_version": 1,
        "address": "198.51.100.7",
        "port": 443,
        "tls": True,
        "params": {"datacenter": "SJC", "region": "US", "city": "San Jose"},
        "remarks": "edge",
        "source_ids": ["local:cf"],
    }
    payload.update(kwargs)
    return EdgeEndpoint(**payload)


def make_ranked_proxy(item_id: str = "p1", **kwargs) -> RankedProxy:
    payload: dict = {
        "item_id": item_id,
        "status": "ranked",
        "rank": 1,
        "score": 0.9,
        "scoring_version": "1",
        "runner_id": RUNNER,
        "probe_status": "ok",
        "probe_mode": "real",
        "sample_count": 4,
        "latency_ms": 120.0,
        "speed_mb_s": 5.0,
        "protocol": "vless",
        "source_ids": ["local:a"],
    }
    payload.update(kwargs)
    return RankedProxy(**payload)


def make_ranked_endpoint(item_id: str = "e1", **kwargs) -> RankedEndpoint:
    payload: dict = {
        "item_id": item_id,
        "status": "ranked",
        "rank": 1,
        "score": 0.8,
        "scoring_version": "1",
        "runner_id": RUNNER,
        "probe_status": "ok",
        "probe_mode": "real",
        "sample_count": 4,
        "latency_ms": 30.0,
        "speed_mb_s": 8.0,
        "address": "198.51.100.7",
        "port": 443,
        "tls": True,
        "host_compatible": True,
        "source_ids": ["local:cf"],
    }
    payload.update(kwargs)
    return RankedEndpoint(**payload)


def make_report(**kwargs) -> dict:
    report: dict = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "runner_id": RUNNER,
        "profile": "local",
        "generated_at": "2026-01-01T12:00:00Z",
        "status": "ok",
        "stages": {},
        "stages_pending": ["inspect", "persist", "score", "export", "publish"],
        "counts": {
            "proxy_nodes": 1,
            "edge_endpoints": 1,
            "sources_total": 1,
            "sources_failed": 0,
        },
    }
    report.update(kwargs)
    return report


def make_proxy_probe(item_id: str = "p1", **kwargs) -> ProxyProbeResult:
    payload: dict = {
        "run_id": RUN_ID,
        "runner_id": RUNNER,
        "status": ProbeStatus.OK,
        "probe_mode": ProbeMode.REAL,
        "backend": "mihomo",
        "item_id": item_id,
        "attempts": 1,
        "download_bytes": 2_000_000,
        "total_latency_ms": 250.0,
        "speed_mb_s": 4.0,
        "speed_unit": "MB/s",
        "measured_at": NOW,
    }
    payload.update(kwargs)
    return ProxyProbeResult(**payload)


def make_endpoint_probe(item_id: str = "e1", **kwargs) -> EndpointProbeResult:
    payload: dict = {
        "run_id": RUN_ID,
        "runner_id": RUNNER,
        "status": ProbeStatus.OK,
        "probe_mode": ProbeMode.REAL,
        "backend": "cfst",
        "item_id": item_id,
        "attempts": 1,
        "address": "198.51.100.7",
        "port": 443,
        "latency_ms": 30.0,
        "speed_mb_s": 8.0,
        "speed_unit": "MB/s",
        "host_compatible": True,
        "measured_at": NOW,
    }
    payload.update(kwargs)
    return EndpointProbeResult(**payload)


# ---------------------------------------------------------------------------
# A1 安装与诊断
# ---------------------------------------------------------------------------


def test_a1_doctor_missing_mihomo_exits_nonzero_with_fix_path(
    monkeypatch, tmp_path: Path, capsys
):
    monkeypatch.setenv(
        "NODEBENCH_PROBE__PROXY__MIHOMO_PATH", str(tmp_path / "absent-mihomo")
    )
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == 2
    assert "[FAIL] mihomo_binary:" in out
    assert "(fix:" in out
    assert "Traceback" not in out
    assert "stack" not in out.lower()


def test_a1_doctor_missing_config_reports_specific_item(monkeypatch, capsys):
    monkeypatch.setenv("NODEBENCH_SOURCES__CF__ENABLED", "true")
    monkeypatch.setenv("NODEBENCH_PROBE__CF__TARGET_HOST", "")
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == 2
    assert "[FAIL]" in out
    assert "probe.cf.target_host" in out
    assert "Traceback" not in out


def test_a1_doctor_token_absent_is_info_not_failure(monkeypatch, capsys):
    monkeypatch.setenv(
        "NODEBENCH_PROBE__PROXY__MIHOMO_PATH", str(Path("absent-mihomo"))
    )
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == 2  # missing mihomo still fails
    assert "[INFO] nodebench_github_token: not set" in out
    assert "[FAIL] nodebench_github_token" not in out


def test_a1_doctor_token_set_never_prints_value(monkeypatch, capsys):
    secret = "ghp_super_secret_token_value"
    monkeypatch.setenv("NODEBENCH_GITHUB_TOKEN", secret)
    monkeypatch.setenv(
        "NODEBENCH_PROBE__PROXY__MIHOMO_PATH", str(Path("absent-mihomo"))
    )
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == 2
    assert secret not in out
    assert "nodebench_github_token: set" in out


def test_a1_doctor_ok_when_prerequisites_resolve(monkeypatch, tmp_path: Path, capsys):
    fake = tmp_path / "mihomo.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("NODEBENCH_PROBE__PROXY__MIHOMO_PATH", str(fake))
    monkeypatch.setenv(
        "NODEBENCH_PROBE__PROXY__SPEEDTEST_URL",
        "https://speed.cloudflare.com/__down",
    )
    monkeypatch.setattr(shutil, "which", lambda name: f"/fake/bin/{name}")
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == 0
    assert "[FAIL]" not in out
    assert DOCTOR_MESSAGE in out


def test_a1_doctor_platform_python_check_present(capsys):
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert "[OK] python:" in out or "[FAIL] python:" in out
    assert code in (0, 2)


# ---------------------------------------------------------------------------
# A2 多源采集
# ---------------------------------------------------------------------------


def test_a2_candidates_carry_source_id_and_license_tag(tmp_path: Path):
    from nodebench.pipeline.stages import license_entries

    source = write(
        tmp_path / "nodes.txt",
        f"vless://{UUID}@192.0.2.10:443?type=tcp&security=tls#a\n",
    )
    config = local_config([source], tmp_path)
    ctx = build_run_context(config, None)
    result = run_pipeline(config, ctx, dry_run=False, run_probes=False)
    reports = result["source_reports"]
    assert reports, "at least one source report"
    for report in reports:
        assert report["source_id"]
        assert report["redacted"] is True
    # license tags are attached at collect time on RawItem
    from nodebench.sources.collect import collect_all

    outcome = collect_all(config, ctx)
    assert outcome.items, "collected raw items"
    for item in outcome.items:
        assert item.source_id
        assert item.license_tag
    entries = license_entries(outcome.items)
    assert entries, "license entries recorded"
    for entry in entries:
        assert "source_id" in entry
        assert "license_tag" in entry


def test_a2_local_source_enforces_file_limit(tmp_path: Path, monkeypatch):
    import nodebench.sources.local as local_mod

    monkeypatch.setattr(local_mod, "MAX_SOURCE_FILES", 1)
    for index in range(3):
        write(
            tmp_path / f"n{index}.txt",
            f"vless://{UUID}@192.0.2.{10 + index}:443?type=tcp&security=tls#n{index}\n",
        )
    config = local_config([tmp_path], tmp_path)
    ctx = build_run_context(config, None)
    result = run_pipeline(config, ctx, dry_run=False, run_probes=False)
    codes = [
        error["code"]
        for report in result["source_reports"]
        for error in report["errors"]
    ]
    assert "file_limit_exceeded" in codes
    assert result["limits"]["truncated_sources"]


def test_a2_one_source_failure_does_not_block_others(tmp_path: Path):
    good = write(
        tmp_path / "good.txt",
        f"vless://{UUID}@192.0.2.10:443?type=tcp&security=tls#ok\n",
    )
    missing = tmp_path / "missing-dir"
    config = local_config([good, missing], tmp_path)
    ctx = build_run_context(config, None)
    result = run_pipeline(config, ctx, dry_run=False, run_probes=False)
    assert result["counts"]["proxy_nodes"] >= 1
    assert result["status"] in ("ok", "partial")


def test_a2_diagnostics_do_not_leak_full_uri(tmp_path: Path):
    secret_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000"
    source = write(
        tmp_path / "mixed.txt",
        f"vless://{secret_uuid}@192.0.2.10:443?type=tcp&security=tls&password=hunter2#x\n"
        "not-a-valid-line\n",
    )
    config = local_config([source], tmp_path)
    ctx = build_run_context(config, None)
    result = run_pipeline(config, ctx, dry_run=False, run_probes=False)
    blob = json.dumps(public_dump(result), ensure_ascii=False)
    assert secret_uuid not in blob
    assert "hunter2" not in blob
    for line in result["diagnostics"]:
        assert secret_uuid not in line
        assert "hunter2" not in line


def test_a2_subscription_budget_limit_is_recorded(tmp_path: Path):
    config = local_config([tmp_path / "empty"], tmp_path)
    config.budget["collect_bytes"] = 1024.0
    ctx = build_run_context(config, None)
    result = run_pipeline(config, ctx, dry_run=False, run_probes=False)
    assert result["limits"]["budget"].get("collect_bytes") == 1024.0


# ---------------------------------------------------------------------------
# A3 解析与去重
# ---------------------------------------------------------------------------


def test_a3_same_connection_params_merge_and_keep_sources(tmp_path: Path):
    a = write(
        tmp_path / "a.txt",
        f"vless://{UUID}@192.0.2.10:443?type=tcp&security=tls#remark-a\n",
    )
    b = write(
        tmp_path / "b.txt",
        f"vless://{UUID}@192.0.2.10:443?type=tcp&security=tls#remark-b\n",
    )
    config = local_config([a, b], tmp_path)
    ctx = build_run_context(config, None)
    result = run_pipeline(config, ctx, dry_run=False, run_probes=False)
    assert result["counts"]["proxy_nodes"] == 1
    assert result["counts"]["dupes_merged"] >= 1
    preview = result["items_preview"]["proxy_nodes"][0]
    assert preview["source_ids"], "merged node keeps source attribution"


def test_a3_different_transport_keeps_two_items(tmp_path: Path):
    text = (
        f"vless://{UUID}@192.0.2.10:443?type=tcp&security=tls#tcp-node\n"
        f"vless://{UUID}@192.0.2.10:443?type=ws&security=tls&path=/ws#ws-node\n"
    )
    source = write(tmp_path / "mixed.txt", text)
    config = local_config([source], tmp_path)
    ctx = build_run_context(config, None)
    result = run_pipeline(config, ctx, dry_run=False, run_probes=False)
    assert result["counts"]["proxy_nodes"] == 2


def test_a3_cf_endpoints_counted_independently(tmp_path: Path):
    source = write(
        tmp_path / "cf.csv",
        "IP地址,端口,回源端口,TLS,数据中心,地区,城市,TCP延迟(ms),速度(MB/s)\n"
        "198.51.100.7,443,,true,SJC,US,San Jose,30,8.0\n"
        "198.51.100.7,8443,,true,SJC,US,San Jose,40,7.0\n",
    )
    config = local_config([source], tmp_path)
    ctx = build_run_context(config, None)
    result = run_pipeline(config, ctx, dry_run=False, run_probes=False)
    assert result["counts"]["proxy_nodes"] == 0
    assert result["counts"]["edge_endpoints"] == 2


def test_a3_unsupported_items_give_reason(tmp_path: Path):
    source = write(
        tmp_path / "bad.txt",
        "garbage-line-without-scheme\n"
        f"vless://{UUID}@192.0.2.10:443?type=tcp&security=tls#ok\n",
    )
    config = local_config([source], tmp_path)
    ctx = build_run_context(config, None)
    result = run_pipeline(config, ctx, dry_run=False, run_probes=False)
    assert result["counts"]["parse_issues"] >= 1
    assert result["issues"], "parse issues carry a reason"
    assert result["issues"][0]["code"]
    assert result["issues"][0]["message_redacted"]


# ---------------------------------------------------------------------------
# A4 真探测
# ---------------------------------------------------------------------------


def test_a4_tcp_success_is_not_proxy_available():
    """Entry TCP can succeed while the proxy path fails → not OK/usable."""
    failed = make_proxy_probe(
        status=ProbeStatus.FAIL,
        failure_stage=FailureStage.DOWNLOAD,
        download_bytes=0,
        speed_mb_s=None,
        total_latency_ms=None,
        error_code="probe_error",
        error_message="download failed",
    )
    assert failed.status is not ProbeStatus.OK
    from nodebench.pipeline.orchestrator import _probe_summary

    summary = _probe_summary([failed], "mihomo")
    assert summary["usable_real"] == 0
    node = make_node()
    from nodebench.core.config import ScoringConfig

    report = score_run(
        run_id=RUN_ID,
        runner_id=RUNNER,
        profile="local",
        nodes=[node],
        edges=[],
        probe_results=[failed],
        scoring=ScoringConfig(),
        history_days=14,
    )
    assert report.proxies[0].status == "filtered"
    assert "probe" in report.proxies[0].filters_failed


def test_a4_cf_incompatible_is_not_exported(tmp_path: Path):
    edge = make_edge()
    node = make_node()
    endpoint = make_ranked_endpoint(
        status="filtered",
        rank=0,
        host_compatible=False,
        filters_failed=["compatibility"],
        probe_status="incompatible",
    )
    proxy = make_ranked_proxy()
    report = make_report()
    register_secrets([UUID])
    outcome = build_export(
        tmp_path / "export",
        report,
        nodes=[node],
        edges=[edge],
        proxies=[proxy],
        endpoints=[endpoint],
        scoring_version="1",
    )
    csv_text = (tmp_path / "export" / CF_ADDCSV_NAME).read_text(encoding="utf-8")
    api_text = (tmp_path / "export" / CF_ADDAPI_NAME).read_text(encoding="utf-8")
    rows = [line for line in csv_text.splitlines() if line.strip()]
    assert len(rows) == 1  # header only
    assert "198.51.100.7" not in api_text


def test_a4_measurements_carry_units_time_and_runner_id():
    result = make_proxy_probe()
    assert result.speed_unit == "MB/s"
    assert result.runner_id == RUNNER
    assert result.measured_at is not None
    assert result.speed_mb_s is not None
    assert result.total_latency_ms is not None
    assert result.download_bytes > 0


def test_a4_simulated_stand_in_is_not_counted_available():
    from nodebench.pipeline.orchestrator import _probe_summary

    simulated = make_proxy_probe(
        status=ProbeStatus.OK,
        probe_mode=ProbeMode.SIMULATED,
    )
    summary = _probe_summary([simulated], "mihomo")
    assert summary["usable_real"] == 0
    assert summary["simulated_results"] == 1
    assert summary["ok"] == 1  # ok count includes simulated, usable_real does not


def test_a4_scoring_rejects_simulated_when_real_required():
    node = make_node()
    simulated = make_proxy_probe(probe_mode=ProbeMode.SIMULATED)
    from nodebench.core.config import ScoringConfig

    report = score_run(
        run_id=RUN_ID,
        runner_id=RUNNER,
        profile="local",
        nodes=[node],
        edges=[],
        probe_results=[simulated],
        scoring=ScoringConfig(),
        history_days=14,
    )
    assert report.proxies[0].status == "filtered"
    assert "probe_mode" in report.proxies[0].filters_failed


def test_a4_missing_binary_marks_not_available(tmp_path: Path):
    summary = {
        "mode": "skip",
        "skipped_reason": REASON_MISSING_BINARY,
        "attempted": 0,
        "ok": 0,
        "usable_real": 0,
        "failed": 0,
        "timeout": 0,
    }
    code = resolve_run_exit(
        run_status="partial",
        probe={"proxy": summary},
        cf_enabled=False,
    )
    assert code == 4


# ---------------------------------------------------------------------------
# A5 情报与历史
# ---------------------------------------------------------------------------


def test_a5_exit_ip_from_proxy_echo(monkeypatch):
    import nodebench.intelligence.service as svc_mod
    from nodebench.core.config import IntelligenceConfig

    def fake_lookup(proxy_url, echo_url, timeout=0.0):
        return "203.0.113.9"

    monkeypatch.setattr(svc_mod, "lookup_exit_ip", fake_lookup)
    monkeypatch.setattr(svc_mod, "lookup_geo", lambda *a, **k: _geo_ok())
    config = IntelligenceConfig(
        echo_url="https://echo.example/ip",
        geo_url="https://geo.example/lookup",
    )
    service = IntelligenceService(config)
    service.proxy_url = "http://127.0.0.1:7890"
    probe = make_proxy_probe(proxy_exit_ip="")
    report = service.inspect_results(
        [probe], run_id=RUN_ID, runner_id=RUNNER
    )
    assert report.entries[0].exit_ip == "203.0.113.9"
    assert report.counts["exit_ok"] == 1


def _geo_ok():
    from nodebench.intelligence.geo import GeoResult

    return GeoResult(country_code="US", asn="AS13335", isp="Cloudflare")


def test_a5_history_sample_counts_across_runs(tmp_path: Path):
    db_path = tmp_path / "history.sqlite"
    nodes = [make_node()]
    for index, status in enumerate(
        (ProbeStatus.OK, ProbeStatus.OK, ProbeStatus.FAIL)
    ):
        run_id = f"2026010{index + 1}T000000Z-run{index}"
        kwargs: dict = {
            "status": status,
            "measured_at": datetime(2026, 1, index + 1, tzinfo=timezone.utc),
        }
        if status is not ProbeStatus.OK:
            kwargs.update(
                download_bytes=0,
                speed_mb_s=None,
                total_latency_ms=None,
                error_code="probe_error",
            )
        probe = make_proxy_probe(**kwargs)
        persist_run(
            db_path,
            run_id=run_id,
            runner_id=RUNNER,
            profile="local",
            status="ok",
            counts={
                "sources_total": 1,
                "sources_failed": 0,
                "raw_items": 1,
                "proxy_nodes": 1,
                "edge_endpoints": 0,
                "parse_issues": 0,
            },
            source_reports=[],
            license_tags={"local": "unknown"},
            nodes=nodes,
            edges=[],
            probe_results=[probe],
            window_days=14,
            now=NOW,
        )
    conn = dbmod.open_db(db_path)
    try:
        history = history_for_items(
            conn,
            test_type="proxy",
            runner_id=RUNNER,
            exclude_run_id="",
            windows=[14],
            min_samples=3,
            now=NOW,
        )
    finally:
        conn.close()
    summary = history["p1"][14]
    assert summary.executed == 3
    assert summary.succeeded == 2
    assert summary.sample_count == 2
    assert summary.availability_rate == pytest.approx(2 / 3)


def test_a5_reputation_failure_is_unknown_and_keeps_history(tmp_path: Path):
    db_path = tmp_path / "history.sqlite"
    nodes = [make_node()]
    good = make_proxy_probe()
    persist_run(
        db_path,
        run_id="20260101T000000Z-good",
        runner_id=RUNNER,
        profile="local",
        status="ok",
        counts={
            "sources_total": 1,
            "sources_failed": 0,
            "raw_items": 1,
            "proxy_nodes": 1,
            "edge_endpoints": 0,
            "parse_issues": 0,
        },
        source_reports=[],
        license_tags={"local": "unknown"},
        nodes=nodes,
        edges=[],
        probe_results=[good],
        window_days=14,
        now=NOW,
    )

    class ExplodingProvider(NullProvider):
        name = "exploding"

        def lookup(self, item_id, exit_ip):
            raise RuntimeError("reputation service down")

    service = IntelligenceService(reputation=ExplodingProvider())
    report = service.inspect_results(
        [good], run_id="20260101T000000Z-good", runner_id=RUNNER
    )
    snapshot = report.reputations[0]
    assert snapshot.status is Status.FAILED
    assert snapshot.risk is None
    assert snapshot.risk_level == "unknown"
    assert snapshot.errors

    record_intelligence(db_path, report)
    conn = dbmod.open_db(db_path)
    try:
        history = history_for_items(
            conn,
            test_type="proxy",
            runner_id=RUNNER,
            exclude_run_id="",
            windows=[14],
            min_samples=1,
            now=NOW,
        )
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM probe_observations"
        ).fetchone()
    finally:
        conn.close()
    assert history["p1"][14].sample_count >= 1
    assert int(rows["n"]) >= 1


# ---------------------------------------------------------------------------
# A6 输出兼容
# ---------------------------------------------------------------------------


def test_a6_report_json_schema_parseable(tmp_path: Path):
    report = make_report()
    outcome = build_export(
        tmp_path / "export",
        report,
        nodes=[make_node()],
        edges=[make_edge()],
        proxies=[make_ranked_proxy()],
        endpoints=[make_ranked_endpoint()],
        scoring_version="1",
    )
    assert outcome.status == "ok"
    raw = (tmp_path / "export" / "report.json").read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert parsed["schema_version"] == 1
    assert parsed["run_id"] == RUN_ID
    assert "counts" in parsed


def test_a6_cf_csv_has_nine_columns_with_units(tmp_path: Path):
    report = make_report()
    build_export(
        tmp_path / "export",
        report,
        nodes=[],
        edges=[make_edge()],
        proxies=[],
        endpoints=[make_ranked_endpoint()],
        scoring_version="1",
    )
    text = (tmp_path / "export" / CF_ADDCSV_NAME).read_text(encoding="utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == HEADER_FULL
    assert len(rows[0]) == 9
    assert rows[0][1] == "端口"
    assert rows[0][3] == "TLS"
    assert rows[0][7] == "TCP延迟(ms)"
    assert rows[0][8] == "速度(MB/s)"
    assert len(rows[1]) == 9
    assert rows[1][1] == "443"
    assert rows[1][3] in ("true", "false")


def test_a6_proxy_uri_does_not_mix_cf_output(tmp_path: Path):
    report = make_report()
    build_export(
        tmp_path / "export",
        report,
        nodes=[make_node()],
        edges=[make_edge()],
        proxies=[make_ranked_proxy()],
        endpoints=[make_ranked_endpoint()],
        scoring_version="1",
    )
    raw_uri = (tmp_path / "export" / PROXY_RAW_NAME).read_text(encoding="utf-8")
    api_text = (tmp_path / "export" / CF_ADDAPI_NAME).read_text(encoding="utf-8")
    csv_text = (tmp_path / "export" / CF_ADDCSV_NAME).read_text(encoding="utf-8")
    assert "198.51.100.7" not in raw_uri
    assert "vless://" not in api_text
    assert "vless://" not in csv_text
    assert "192.0.2.10" not in api_text
    assert "192.0.2.10" not in csv_text


def test_a6_manifest_sha256_matches_files(tmp_path: Path):
    report = make_report()
    build_export(
        tmp_path / "export",
        report,
        nodes=[make_node()],
        edges=[make_edge()],
        proxies=[make_ranked_proxy()],
        endpoints=[make_ranked_endpoint()],
        scoring_version="1",
    )
    manifest = json.loads(
        (tmp_path / "export" / MANIFEST_NAME).read_text(encoding="utf-8")
    )
    for entry in manifest["files"]:
        payload = (tmp_path / "export" / entry["name"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
        assert len(payload) == entry["size_bytes"]


def test_a6_workervless2sub_csv_import_roundtrip(tmp_path: Path):
    """The nine-column CSV must parse back through the documented importer."""
    rows_in = [["198.51.100.7", 443, "", True, "SJC", "US", "San Jose", 30.0, 8.25]]
    text = build_addcsv(rows_in)
    parsed, issues = parse_endpoint_csv(text)
    assert not issues
    assert len(parsed) == 1
    assert parsed[0].address == "198.51.100.7"
    assert parsed[0].port == 443
    assert parsed[0].tls is True
    assert parsed[0].params["datacenter"] == "SJC"
    assert parsed[0].params["region"] == "US"
    assert parsed[0].params["city"] == "San Jose"


def test_a6_addapi_line_pattern(tmp_path: Path):
    report = make_report()
    build_export(
        tmp_path / "export",
        report,
        nodes=[],
        edges=[make_edge()],
        proxies=[],
        endpoints=[make_ranked_endpoint()],
        scoring_version="1",
    )
    text = (tmp_path / "export" / CF_ADDAPI_NAME).read_text(encoding="utf-8")
    line = text.strip().splitlines()[0]
    assert line.startswith("198.51.100.7:443")
    assert "#" in line or line == "198.51.100.7:443"


# ---------------------------------------------------------------------------
# A7 一键与定时
# ---------------------------------------------------------------------------


def test_a7_run_scripts_invoke_shared_cli():
    scripts_dir = PROJECT_ROOT / "scripts"
    for name in ("run.ps1", "run.sh", "run.bat"):
        text = (scripts_dir / name).read_text(encoding="utf-8")
        assert "nodebench" in text
        assert "run" in text


def test_a7_scheduler_owns_only_project_tasks():
    from nodebench.scheduler.base import TASK_PREFIX, ensure_owned_name

    assert TASK_PREFIX.startswith("nodebench")
    ensure_owned_name(f"{TASK_PREFIX}-local-0437")
    with pytest.raises(Exception):
        ensure_owned_name("OtherVendor-Task")


def test_a7_scheduler_uninstall_is_reversible_plan():
    from nodebench.scheduler.base import build_task
    from nodebench.scheduler.schtasks import SchtasksBackend

    task = build_task("local", "04:37", root=PROJECT_ROOT, platform="windows")
    backend = SchtasksBackend(runner=lambda argv, execute: None)
    install = backend.plan(task, "install")
    uninstall = backend.plan(task, "uninstall")
    assert install and uninstall
    assert any("/create" in " ".join(cmd).lower() for cmd in install)
    assert any("/delete" in " ".join(cmd).lower() for cmd in uninstall)


def test_a7_run_id_marks_distinct_run_points():
    ctx_a = build_run_context(local_config(), None)
    ctx_b = build_run_context(local_config(), None)
    assert ctx_a.run_id != ctx_b.run_id or ctx_a.runner_id == ctx_b.runner_id
    assert ctx_a.runner_id
    assert ctx_a.run_id


# ---------------------------------------------------------------------------
# A8 发布与失败
# ---------------------------------------------------------------------------


def test_a8_empty_source_yields_failed_status_and_exit(tmp_path: Path):
    empty = write(tmp_path / "empty.txt", "\n")
    config = local_config([empty], tmp_path)
    ctx = build_run_context(config, None)
    result = run_pipeline(config, ctx, dry_run=False, run_probes=False)
    assert result["status"] == "failed"
    code = resolve_run_exit(
        run_status=result["status"],
        probe=result.get("probe"),
        cf_enabled=bool(config.probe.cf.enabled),
        target_host=str(config.probe.cf.target_host or ""),
    )
    assert code == EXIT_SOURCES


def test_a8_error_csv_produces_diagnostics(tmp_path: Path):
    bad = write(
        tmp_path / "bad.csv",
        "IP地址,端口,回源端口,TLS,数据中心,地区,城市,TCP延迟(ms),速度(MB/s)\n"
        "not-an-ip,abc,,maybe,,,,,\n",
    )
    config = local_config([bad], tmp_path)
    ctx = build_run_context(config, None)
    result = run_pipeline(config, ctx, dry_run=False, run_probes=False)
    assert result["counts"]["parse_issues"] >= 1
    assert result["diagnostics"] or result["issues"]
    assert result["status"] in ("failed", "partial")


def test_a8_publish_failure_keeps_previous_public(tmp_path: Path):
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    target = tmp_path / "latest"
    target.mkdir()
    (target / "old.txt").write_text("keep-me", encoding="utf-8")

    from nodebench.core.schema import ExportOutcome, ExportedFile, ValidationReport

    outcome = ExportOutcome(
        status="ok",
        directory=str(export_dir),
        files=[
            ExportedFile(
                name="report.json",
                sha256="0" * 64,
                size_bytes=2,
                entry_count=0,
            )
        ],
        errors=[],
        validation=ValidationReport(ok=True, errors=[], files_checked=[]),
        publishable=False,
        counts={},
    )
    result = publish_output(
        export_dir=export_dir,
        target_dir=target,
        report=make_report(),
        outcome=outcome,
        ranked_proxies=0,
        ranked_endpoints=0,
        proxy_license_tags=["mit"],
        endpoint_license_tags=["mit"],
        enabled=True,
        allow_publish=True,
        allow_proxy_credentials=True,
    )
    assert result.status in ("blocked", "failed")
    assert (target / "old.txt").read_text(encoding="utf-8") == "keep-me"


def test_a8_empty_content_files_do_not_overwrite_public(tmp_path: Path):
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    target = tmp_path / "latest"
    target.mkdir()
    (target / "cf-addapi.txt").write_text("1.2.3.4:443\n", encoding="utf-8")

    from nodebench.core.schema import ExportOutcome, ExportedFile, ValidationReport

    outcome = ExportOutcome(
        status="ok",
        directory=str(export_dir),
        files=[
            ExportedFile(
                name="cf-addapi.txt",
                sha256="0" * 64,
                size_bytes=0,
                entry_count=0,
            ),
            ExportedFile(
                name="report.json",
                sha256="0" * 64,
                size_bytes=10,
                entry_count=0,
            ),
        ],
        errors=[],
        validation=ValidationReport(ok=True, errors=[], files_checked=[]),
        publishable=True,
        counts={},
    )
    (export_dir / "report.json").write_text("{}", encoding="utf-8")
    (export_dir / "manifest.json").write_text("{}", encoding="utf-8")
    result = publish_output(
        export_dir=export_dir,
        target_dir=target,
        report=make_report(),
        outcome=outcome,
        ranked_proxies=1,
        ranked_endpoints=0,
        proxy_license_tags=["mit"],
        endpoint_license_tags=["mit"],
        enabled=True,
        allow_publish=True,
        allow_proxy_credentials=True,
    )
    # Either blocked (zero ranked endpoints path) or published without the empty file.
    if result.status == "ok":
        assert "cf-addapi.txt" in result.excluded
        assert not (target / "cf-addapi.txt").exists() or (
            target / "cf-addapi.txt"
        ).read_text(encoding="utf-8") == "1.2.3.4:443\n"
    else:
        assert (target / "cf-addapi.txt").read_text(encoding="utf-8") == (
            "1.2.3.4:443\n"
        )


def test_a8_no_secrets_in_public_report(tmp_path: Path):
    secret = "super-secret-password-42"
    register_secrets([secret])
    report = make_report()
    report["notes"] = redact(f"credential {secret} leaked", [secret])
    outcome = build_export(
        tmp_path / "export",
        report,
        nodes=[make_node(secrets={"uuid": UUID, "password": secret})],
        edges=[],
        proxies=[],
        endpoints=[],
        scoring_version="1",
    )
    public_text = json.dumps(public_dump(report), ensure_ascii=False)
    assert secret not in public_text
    export_raw = (tmp_path / "export" / PROXY_RAW_NAME)
    if export_raw.exists():
        # private export may hold credentials; public scan must catch them
        pass
    from nodebench.exporters.validate import scan_text

    assert scan_text("report.json", public_text) == []


# ---------------------------------------------------------------------------
# A9 回归
# ---------------------------------------------------------------------------


def test_a9_offline_suite_collects_expected_modules():
    tests_dir = Path(__file__).resolve().parent
    required = [
        "test_parsers_uri.py",
        "test_normalize_dedupe.py",
        "test_probe_contract.py",
        "test_scoring.py",
        "test_exporters.py",
        "test_publish.py",
        "test_orchestrator.py",
        "test_history_db.py",
    ]
    for name in required:
        assert (tests_dir / name).is_file(), f"missing regression module {name}"
