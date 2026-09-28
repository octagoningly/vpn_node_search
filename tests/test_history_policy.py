"""M4 history policy: retention, scheduled/executed availability, runner isolation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from nodebench.core.config import HistoryConfig, ScoringConfig
from nodebench.core.schema import (
    HistorySummary,
    ProbeMode,
    ProbeStatus,
    ProxyNode,
    ProxyProbeResult,
    SourceReport,
)
from nodebench.history.db import open_db
from nodebench.history.stats import history_for_items, prune_runs
from nodebench.history.store import persist_run
from nodebench.scoring.rank import score_run

RUN1 = "20260901T000000Z-aaaaaa"
RUN2 = "20260902T000000Z-bbbbbb"
RUN3 = "20260903T000000Z-cccccc"
NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


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
        params={"risk": 10.0},
        secrets={"uuid": "123e4567-e89b-12d3-a456-426614174000"},
        source_ids=["local"],
    )


def proxy_result(
    item_id: str = "node-1",
    *,
    run_id: str = RUN1,
    status: ProbeStatus = ProbeStatus.OK,
    measured_at: datetime = NOW,
    runner_id: str = "local:desktop-a",
) -> ProxyProbeResult:
    return ProxyProbeResult(
        run_id=run_id,
        runner_id=runner_id,
        measured_at=measured_at,
        status=status,
        probe_mode=ProbeMode.REAL,
        backend="mihomo",
        skipped_reason="missing_binary" if status is ProbeStatus.SKIPPED else "",
        attempts=0 if status is ProbeStatus.SKIPPED else 1,
        item_id=item_id,
        total_latency_ms=100.0 if status is ProbeStatus.OK else None,
        download_bytes=2_000_000 if status is ProbeStatus.OK else 0,
        speed_mb_s=3.0 if status is ProbeStatus.OK else None,
    )


def persist(
    db_path: Path,
    *,
    run_id: str,
    now: datetime,
    results: list | None = None,
    nodes: list | None = None,
    runner_id: str = "local:desktop-a",
    window_days: int = 14,
    retention_days: int | None = None,
    source_items_for: list[str] | None = None,
):
    """Persist one run; ``source_items_for`` forces extra scheduled item ids."""
    used_nodes = nodes if nodes is not None else [make_node()]
    summary = persist_run(
        db_path,
        run_id=run_id,
        runner_id=runner_id,
        profile="local",
        status="ok",
        counts={
            "sources_total": 1,
            "sources_failed": 0,
            "raw_items": 1,
            "proxy_nodes": len(used_nodes),
            "edge_endpoints": 0,
            "parse_issues": 0,
        },
        source_reports=[SourceReport(source_id="local", ok=True, fetched=1)],
        license_tags={"local": "unknown"},
        nodes=used_nodes,
        edges=[],
        probe_results=results if results is not None else [],
        window_days=window_days,
        retention_days=retention_days,
        now=now,
    )
    if source_items_for:
        conn = open_db(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for item_id in source_items_for:
                conn.execute(
                    "INSERT OR IGNORE INTO source_items (run_id, source_id, item_id) "
                    "VALUES (?, ?, ?)",
                    (run_id, "local", item_id),
                )
            conn.execute("COMMIT")
        finally:
            conn.close()
    return summary


def test_retention_days_prunes_independently_of_window(tmp_path: Path):
    db_path = tmp_path / "nodebench.db"
    old = NOW - timedelta(days=20)
    mid = NOW - timedelta(days=10)
    persist(db_path, run_id=RUN1, now=old, results=[proxy_result(measured_at=old)])
    persist(db_path, run_id=RUN2, now=mid, results=[proxy_result(measured_at=mid)])
    conn = open_db(db_path)
    try:
        # window_days=14 would drop RUN1; a longer retention keeps it.
        pruned = prune_runs(conn, window_days=30, now=NOW)
        assert pruned == 0
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    finally:
        conn.close()
    summary = persist(
        db_path,
        run_id=RUN3,
        now=NOW,
        results=[proxy_result(run_id=RUN3)],
        window_days=7,
        retention_days=30,
    )
    assert summary.pruned == 0
    conn = open_db(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3
    finally:
        conn.close()


def test_retention_days_shorter_than_window_prunes(tmp_path: Path):
    db_path = tmp_path / "nodebench.db"
    old = NOW - timedelta(days=20)
    persist(db_path, run_id=RUN1, now=old, results=[proxy_result(measured_at=old)])
    summary = persist(
        db_path,
        run_id=RUN2,
        now=NOW,
        results=[proxy_result(run_id=RUN2)],
        window_days=14,
        retention_days=7,
    )
    assert summary.pruned == 1
    conn = open_db(db_path)
    try:
        rows = conn.execute("SELECT run_id FROM runs").fetchall()
        assert [str(row["run_id"]) for row in rows] == [RUN2]
    finally:
        conn.close()


def test_missing_observation_is_neither_success_nor_failure(tmp_path: Path):
    """Scheduled-but-not-executed runs stay out of the availability ratio."""
    db_path = tmp_path / "nodebench.db"
    earlier = NOW - timedelta(days=1)
    persist(
        db_path,
        run_id=RUN1,
        now=earlier,
        results=[proxy_result(run_id=RUN1, measured_at=earlier)],
    )
    # RUN2 schedules the same item but produces no probe observation at all.
    persist(
        db_path,
        run_id=RUN2,
        now=NOW,
        results=[],
        nodes=[make_node()],
        source_items_for=["node-1"],
    )
    conn = open_db(db_path)
    try:
        past = history_for_items(
            conn,
            test_type="proxy",
            runner_id="local:desktop-a",
            exclude_run_id=RUN2,
            windows=[7],
            min_samples=3,
            now=NOW,
        )
        # When RUN2 is excluded, only RUN1's executed observation counts.
        summary = past["node-1"][7]
        assert summary.scheduled == 1
        assert summary.executed == 1
        assert summary.succeeded == 1
        assert summary.availability_rate == 1.0

        including_missing = history_for_items(
            conn,
            test_type="proxy",
            runner_id="local:desktop-a",
            exclude_run_id="20260904T000000Z-dddddd",
            windows=[7],
            min_samples=3,
            now=NOW,
        )
        summary = including_missing["node-1"][7]
        # RUN2 is scheduled but never executed: it must not dilute the rate.
        assert summary.scheduled == 2
        assert summary.executed == 1
        assert summary.succeeded == 1
        assert summary.availability_rate == 1.0
        assert summary.sample_count == 1
    finally:
        conn.close()


def test_failed_day_and_gap_day_do_not_fake_zero_or_one(tmp_path: Path):
    """Days with no scheduled run are not failures; a fail run is one fail."""
    db_path = tmp_path / "nodebench.db"
    day0 = NOW - timedelta(days=3)
    day1 = NOW - timedelta(days=2)
    # day1 has no run at all (gap). day0 is a measured failure.
    persist(
        db_path,
        run_id=RUN1,
        now=day0,
        results=[
            proxy_result(
                run_id=RUN1,
                status=ProbeStatus.FAIL,
                measured_at=day0,
            )
        ],
    )
    conn = open_db(db_path)
    try:
        past = history_for_items(
            conn,
            test_type="proxy",
            runner_id="local:desktop-a",
            exclude_run_id="20260904T000000Z-dddddd",
            windows=[7],
            min_samples=1,
            now=NOW,
        )
        summary = past["node-1"][7]
        assert summary.scheduled == 1
        assert summary.executed == 1
        assert summary.succeeded == 0
        assert summary.availability_rate == 0.0
    finally:
        conn.close()


def test_runner_id_history_is_isolated(tmp_path: Path):
    db_path = tmp_path / "nodebench.db"
    earlier = NOW - timedelta(days=1)
    persist(
        db_path,
        run_id=RUN1,
        now=earlier,
        results=[proxy_result(run_id=RUN1, measured_at=earlier)],
        runner_id="local:desktop-a",
    )
    persist(
        db_path,
        run_id=RUN2,
        now=NOW,
        results=[proxy_result(run_id=RUN2, runner_id="github:actions")],
        runner_id="github:actions",
    )
    conn = open_db(db_path)
    try:
        local = history_for_items(
            conn,
            test_type="proxy",
            runner_id="local:desktop-a",
            exclude_run_id="20260904T000000Z-dddddd",
            windows=[7],
            min_samples=1,
            now=NOW,
        )
        actions = history_for_items(
            conn,
            test_type="proxy",
            runner_id="github:actions",
            exclude_run_id="20260904T000000Z-dddddd",
            windows=[7],
            min_samples=1,
            now=NOW,
        )
        assert local["node-1"][7].executed == 1
        assert local["node-1"][7].succeeded == 1
        assert actions["node-1"][7].executed == 1
        assert actions["node-1"][7].succeeded == 1
        # Neither runner sees the other's observation as its own second sample.
        assert local["node-1"][7].sample_count == 1
        assert actions["node-1"][7].sample_count == 1
    finally:
        conn.close()


def test_stability_insufficient_samples_penalize_explicitly():
    """Sample-count gate: stability is dropped and the score is demoted."""
    node = make_node()
    result = proxy_result()
    history = {
        "node-1": {
            14: HistorySummary(
                item_id="node-1",
                window_days=14,
                scheduled=2,
                executed=2,
                succeeded=2,
                sample_count=2,
                availability_rate=1.0,
                min_samples=3,
            )
        }
    }
    report = score_run(
        run_id="20260928T120000Z-abcdef",
        runner_id="local:desktop-a",
        profile="local",
        nodes=[node],
        edges=[],
        probe_results=[result],
        proxy_history=history,
        scoring=ScoringConfig(),
        history_days=14,
    )
    item = report.proxies[0]
    assert item.status == "ranked"
    assert item.notes["stability_samples_insufficient"] is True
    assert item.notes["stability_penalty"] == 0.9
    assert "stability" not in item.score_breakdown
    assert item.score < 1.0
    # Enough samples: stability participates and no penalty note remains.
    history["node-1"][14] = HistorySummary(
        item_id="node-1",
        window_days=14,
        scheduled=5,
        executed=5,
        succeeded=5,
        sample_count=5,
        availability_rate=1.0,
        min_samples=3,
    )
    report = score_run(
        run_id="20260928T120000Z-abcdef",
        runner_id="local:desktop-a",
        profile="local",
        nodes=[node],
        edges=[],
        probe_results=[result],
        proxy_history=history,
        scoring=ScoringConfig(),
        history_days=14,
    )
    item = report.proxies[0]
    assert item.notes.get("stability_samples_insufficient") is None
    assert "stability" in item.score_breakdown
    assert item.score_breakdown["stability"] == 1.0


def test_history_config_retention_is_optional_and_validated():
    default = HistoryConfig()
    assert default.days == 14
    assert default.retention_days is None
    assert HistoryConfig(days=7, retention_days=30).retention_days == 30
    for bad in (0, -1, True):
        try:
            HistoryConfig(days=bad)  # type: ignore[arg-type]
        except ValueError:
            pass
        else:
            raise AssertionError(f"days={bad!r} should be rejected")
    try:
        HistoryConfig(retention_days=0)
    except ValueError:
        pass
    else:
        raise AssertionError("retention_days=0 should be rejected")
