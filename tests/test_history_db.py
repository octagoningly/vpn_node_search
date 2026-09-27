from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nodebench.core.errors import StorageError
from nodebench.core.schema import (
    EdgeEndpoint,
    EndpointProbeResult,
    HistorySummary,
    ProbeMode,
    ProbeStatus,
    ProxyNode,
    ProxyProbeResult,
    SourceReport,
)
from nodebench.history.db import open_db
from nodebench.history.stats import history_for_items, prune_runs, source_quality
from nodebench.history.store import decode_probe_results, load_entities, persist_run

TABLES = (
    "schema_migrations",
    "runs",
    "sources",
    "items",
    "source_items",
    "probe_observations",
    "exit_observations",
    "reputation_observations",
)

RUN1 = "20260901T000000Z-aaaaaa"
RUN2 = "20260902T000000Z-bbbbbb"
NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)


def make_node(item_id: str = "node-1") -> ProxyNode:
    return ProxyNode(
        item_id=item_id,
        kind="proxy_node",
        fingerprint=f"fp-{item_id}",
        fingerprint_version=1,
        protocol="vless",
        server="192.0.2.10",
        port=443,
        transport="tcp",
        security="tls",
        params={"network": "tcp"},
        secrets={"uuid": "123e4567-e89b-12d3-a456-426614174000"},
        remarks="sample",
        source_ids=["local"],
        raw_refs=["nodes.txt"],
    )


def make_edge(item_id: str = "edge-1") -> EdgeEndpoint:
    return EdgeEndpoint(
        item_id=item_id,
        kind="edge_endpoint",
        fingerprint=f"fp-{item_id}",
        fingerprint_version=1,
        address="198.51.100.7",
        port=443,
        target_host="edge.example.test",
        tls=True,
        params={"region": "HKG"},
        source_ids=["cf"],
        raw_refs=["cf-candidates.csv"],
    )


def proxy_result(
    item_id: str = "node-1",
    *,
    run_id: str = RUN1,
    status: ProbeStatus = ProbeStatus.OK,
    measured_at: datetime = NOW,
    latency: float | None = 120.0,
    exit_ip: str = "203.0.113.9",
    skipped_reason: str = "",
    probe_mode: ProbeMode = ProbeMode.REAL,
) -> ProxyProbeResult:
    return ProxyProbeResult(
        run_id=run_id,
        runner_id="local:desktop-a",
        measured_at=measured_at,
        status=status,
        probe_mode=probe_mode,
        backend="mihomo",
        skipped_reason=skipped_reason,
        attempts=1 if status is not ProbeStatus.SKIPPED else 0,
        timeouts=1 if status is ProbeStatus.TIMEOUT else 0,
        error_code="probe_error" if status is ProbeStatus.FAIL else "",
        item_id=item_id,
        total_latency_ms=latency,
        download_bytes=1_048_576 if latency is not None else 0,
        speed_mb_s=2.5 if latency is not None else None,
        proxy_exit_ip=exit_ip,
    )


def endpoint_result(
    item_id: str = "edge-1",
    *,
    run_id: str = RUN1,
    status: ProbeStatus = ProbeStatus.OK,
    measured_at: datetime = NOW,
) -> EndpointProbeResult:
    return EndpointProbeResult(
        run_id=run_id,
        runner_id="local:desktop-a",
        measured_at=measured_at,
        status=status,
        probe_mode=ProbeMode.REAL,
        backend="cfst",
        attempts=1 if status is not ProbeStatus.SKIPPED else 0,
        item_id=item_id,
        address="198.51.100.7",
        port=443,
        target_host="edge.example.test",
        tls=True,
        tcp_ok=True,
        https_ok=True,
        host_compatible=True,
        loss_pct=0.0,
        latency_ms=42.0,
        speed_mb_s=8.0,
        region="HKG",
    )


def persist(
    db_path: Path,
    *,
    run_id: str,
    now: datetime,
    nodes: list[ProxyNode] | None = None,
    edges: list[EdgeEndpoint] | None = None,
    results: list | None = None,
) -> object:
    return persist_run(
        db_path,
        run_id=run_id,
        runner_id="local:desktop-a",
        profile="local",
        status="partial" if results else "ok",
        counts={
            "sources_total": 1,
            "sources_failed": 0,
            "raw_items": 1,
            "proxy_nodes": len(nodes or []),
            "edge_endpoints": len(edges or []),
            "parse_issues": 0,
        },
        source_reports=[SourceReport(source_id="local", ok=True, fetched=1)],
        license_tags={"local": "unknown"},
        nodes=nodes if nodes is not None else [make_node()],
        edges=edges if edges is not None else [],
        probe_results=results if results is not None else [],
        window_days=14,
        now=now,
    )


def test_persist_run_creates_all_tables_and_summary(tmp_path: Path):
    db_path = tmp_path / "nodebench.db"
    summary = persist(
        db_path,
        run_id=RUN1,
        now=NOW,
        nodes=[make_node(), make_node("node-2")],
        edges=[make_edge()],
        results=[
            proxy_result(),
            endpoint_result(),
            proxy_result(
                "node-2",
                status=ProbeStatus.SKIPPED,
                latency=None,
                exit_ip="",
                skipped_reason="missing_binary",
            ),
        ],
    )
    assert summary.status == "ok"
    assert summary.runs == 1
    assert summary.sources == 1
    assert summary.items == 3
    assert summary.observations == 3
    assert summary.pruned == 0
    assert summary.database == str(db_path)
    assert summary.error == ""
    assert summary.source_quality == []
    conn = open_db(db_path)
    try:
        names = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in TABLES:
            assert table in names, table
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 3
        assert (
            conn.execute("SELECT COUNT(*) FROM probe_observations").fetchone()[0] == 3
        )
        assert conn.execute("SELECT COUNT(*) FROM exit_observations").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        row = conn.execute(
            "SELECT payload FROM items WHERE item_id='node-1'"
        ).fetchone()
        assert "123e4567-e89b-12d3-a456-426614174000" in str(row["payload"])
    finally:
        conn.close()


def test_persist_run_is_idempotent_per_run(tmp_path: Path):
    db_path = tmp_path / "nodebench.db"
    first = persist(db_path, run_id=RUN1, now=NOW, results=[proxy_result()])
    second = persist(db_path, run_id=RUN1, now=NOW, results=[proxy_result()])
    assert first.observations == 1
    assert second.observations == 1
    conn = open_db(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM probe_observations").fetchone()[0] == 1
        first_seen, last_seen = conn.execute(
            "SELECT first_seen_run, last_seen_run FROM items WHERE item_id='node-1'"
        ).fetchone()
        assert first_seen == RUN1
        assert last_seen == RUN1
    finally:
        conn.close()


def test_history_windows_exclude_current_run(tmp_path: Path):
    db_path = tmp_path / "nodebench.db"
    earlier = NOW - timedelta(days=2)
    persist(
        db_path,
        run_id=RUN1,
        now=earlier,
        results=[proxy_result(run_id=RUN1, measured_at=earlier)],
    )
    persist(
        db_path,
        run_id=RUN2,
        now=NOW,
        results=[
            proxy_result(
                run_id=RUN2,
                status=ProbeStatus.FAIL,
                latency=None,
                exit_ip="",
            )
        ],
    )
    conn = open_db(db_path)
    try:
        past = history_for_items(
            conn,
            test_type="proxy",
            runner_id="local:desktop-a",
            exclude_run_id=RUN2,
            windows=[7, 14],
            min_samples=3,
            now=NOW,
        )
        entry = past["node-1"]
        for days in (7, 14):
            summary = entry[days]
            assert isinstance(summary, HistorySummary)
            assert summary.window_days == days
            assert summary.scheduled == 1
            assert summary.executed == 1
            assert summary.succeeded == 1
            assert summary.sample_count == 1
            assert summary.availability_rate == 1.0
            assert summary.min_samples == 3
            assert summary.first_observed_at is not None
        current = history_for_items(
            conn,
            test_type="proxy",
            runner_id="local:desktop-a",
            exclude_run_id=RUN1,
            windows=[7, 14],
            min_samples=3,
            now=NOW,
        )
        assert current["node-1"][7].availability_rate == 0.0
        quality = source_quality(
            conn,
            runner_id="local:desktop-a",
            window_days=14,
            exclude_run_id=RUN2,
            now=NOW,
        )
        assert len(quality) == 1
        assert quality[0].runs_seen == 1
    finally:
        conn.close()


def test_skipped_results_do_not_count_as_executed(tmp_path: Path):
    db_path = tmp_path / "nodebench.db"
    persist(
        db_path,
        run_id=RUN1,
        now=NOW,
        results=[
            proxy_result(
                status=ProbeStatus.SKIPPED,
                latency=None,
                exit_ip="",
                skipped_reason="missing_binary",
            )
        ],
    )
    conn = open_db(db_path)
    try:
        past = history_for_items(
            conn,
            test_type="proxy",
            runner_id="local:desktop-a",
            exclude_run_id="20260903T000000Z-cccccc",
            windows=[7],
            min_samples=3,
            now=NOW,
        )
        summary = past["node-1"][7]
        assert summary.scheduled == 1
        assert summary.executed == 0
        assert summary.succeeded == 0
        assert summary.availability_rate is None
    finally:
        conn.close()


def test_skipped_result_persists_not_run_probe_mode(tmp_path: Path):
    db_path = tmp_path / "nodebench.db"
    persist(
        db_path,
        run_id=RUN1,
        now=NOW,
        results=[
            proxy_result(
                status=ProbeStatus.SKIPPED,
                latency=None,
                exit_ip="",
                skipped_reason="missing_binary",
                probe_mode=ProbeMode.NOT_RUN,
            )
        ],
    )
    conn = open_db(db_path)
    try:
        row = conn.execute(
            "SELECT status, probe_mode, attempts, skipped_reason "
            "FROM probe_observations WHERE item_id='node-1'"
        ).fetchone()
        assert row["status"] == "skipped"
        assert row["probe_mode"] == "not_run"
        assert row["attempts"] == 0
        assert row["skipped_reason"] == "missing_binary"
    finally:
        conn.close()


def test_prune_removes_old_runs_and_keeps_items(tmp_path: Path):
    db_path = tmp_path / "nodebench.db"
    old_now = NOW - timedelta(days=30)
    persist(
        db_path,
        run_id=RUN1,
        now=old_now,
        results=[proxy_result(run_id=RUN1, measured_at=old_now)],
    )
    conn = open_db(db_path)
    try:
        pruned = prune_runs(conn, window_days=14, now=NOW)
        assert pruned == 1
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM probe_observations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    finally:
        conn.close()
    persist(
        db_path,
        run_id=RUN2,
        now=NOW,
        results=[proxy_result(run_id=RUN2, measured_at=NOW)],
    )
    conn = open_db(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM probe_observations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        assert prune_runs(conn, window_days=14, now=NOW) == 0
    finally:
        conn.close()


def test_load_entities_roundtrip_including_secrets(tmp_path: Path):
    db_path = tmp_path / "nodebench.db"
    persist(db_path, run_id=RUN1, now=NOW, nodes=[make_node()], edges=[make_edge()])
    nodes, edges = load_entities(db_path, RUN1)
    assert [node.item_id for node in nodes] == ["node-1"]
    assert nodes[0].secrets["uuid"] == "123e4567-e89b-12d3-a456-426614174000"
    assert [edge.item_id for edge in edges] == ["edge-1"]
    assert edges[0].tls is True
    persist(db_path, run_id=RUN2, now=NOW, nodes=[], edges=[make_edge()])
    nodes, edges = load_entities(db_path, RUN2)
    assert nodes == []
    assert [edge.item_id for edge in edges] == ["edge-1"]
    with pytest.raises(StorageError):
        load_entities(db_path, "20260101T000000Z-zzzzzz")


def test_missing_database_raises_storage_error(tmp_path: Path):
    with pytest.raises(StorageError):
        load_entities(tmp_path / "absent.db", RUN1)


def test_persist_failure_raises_storage_error(tmp_path: Path):
    blocker = tmp_path / "nodebench.db"
    blocker.write_text("not a database", encoding="utf-8")
    with pytest.raises(StorageError):
        persist(blocker, run_id=RUN1, now=NOW, results=[proxy_result()])


def test_decode_probe_results_roundtrip(tmp_path: Path):
    db_path = tmp_path / "nodebench.db"
    persist(db_path, run_id=RUN1, now=NOW, results=[proxy_result(), endpoint_result()])
    payload = {
        "schema_version": 1,
        "run_id": RUN1,
        "results": [
            proxy_result().model_dump(mode="json"),
            endpoint_result().model_dump(mode="json"),
        ],
    }
    results = decode_probe_results(payload)
    assert [type(item) for item in results] == [ProxyProbeResult, EndpointProbeResult]
    with pytest.raises(StorageError):
        decode_probe_results({"results": [{"kind": "mystery"}]})


def test_decode_probe_results_rejects_non_objects():
    with pytest.raises(StorageError):
        decode_probe_results({"results": ["nope"]})


def test_persist_rejects_bad_window(tmp_path: Path):
    db_path = tmp_path / "nodebench.db"
    persist(db_path, run_id=RUN1, now=NOW)
    conn = open_db(db_path)
    try:
        with pytest.raises(ValueError):
            prune_runs(conn, window_days=0, now=NOW)
        with pytest.raises(ValueError):
            history_for_items(
                conn,
                test_type="proxy",
                runner_id="local:desktop-a",
                exclude_run_id=RUN1,
                windows=[],
                min_samples=3,
                now=NOW,
            )
    finally:
        conn.close()
