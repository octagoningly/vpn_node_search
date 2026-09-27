from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nodebench.core.errors import StorageError
from nodebench.core.schema import (
    EdgeEndpoint,
    EndpointProbeResult,
    PersistSummary,
    ProxyNode,
    ProxyProbeResult,
    SourceReport,
)
from nodebench.history import db as dbmod
from nodebench.history.stats import prune_runs, source_quality

PROXY_TEST_TYPE = "proxy"
ENDPOINT_TEST_TYPE = "cf"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _speed_mb_s(result: ProxyProbeResult) -> float | None:
    if result.speed_mb_s is not None:
        return float(result.speed_mb_s)
    if result.speed_mbps is not None:
        return float(result.speed_mbps) / 8.0
    return None


def _observation_rows(
    run_id: str,
    runner_id: str,
    probe_results: Sequence[Any],
    now: datetime,
) -> tuple[list[tuple], list[tuple]]:
    rows: list[tuple] = []
    exits: list[tuple] = []
    for result in probe_results:
        if isinstance(result, ProxyProbeResult):
            test_type = PROXY_TEST_TYPE
            latency = (
                float(result.total_latency_ms)
                if result.total_latency_ms is not None
                else None
            )
            speed = _speed_mb_s(result)
            loss = (
                100.0 * result.timeouts / result.attempts
                if result.attempts > 0
                else None
            )
            rows.append(
                (
                    str(result.item_id),
                    runner_id,
                    run_id,
                    test_type,
                    dbmod.utc_stamp(result.measured_at),
                    str(result.status.value),
                    str(result.probe_mode.value),
                    str(result.backend),
                    str(result.skipped_reason),
                    int(result.attempts),
                    int(result.timeouts),
                    latency,
                    speed,
                    str(result.speed_unit),
                    loss,
                    str(result.error_code),
                )
            )
            if result.proxy_exit_ip:
                exits.append(
                    (
                        str(result.item_id),
                        runner_id,
                        run_id,
                        test_type,
                        dbmod.utc_stamp(result.measured_at),
                        str(result.proxy_exit_ip),
                        None,
                        None,
                        None,
                    )
                )
        elif isinstance(result, EndpointProbeResult):
            rows.append(
                (
                    str(result.item_id),
                    runner_id,
                    run_id,
                    ENDPOINT_TEST_TYPE,
                    dbmod.utc_stamp(result.measured_at),
                    str(result.status.value),
                    str(result.probe_mode.value),
                    str(result.backend),
                    str(result.skipped_reason),
                    int(result.attempts),
                    int(result.timeouts),
                    float(result.latency_ms) if result.latency_ms is not None else None,
                    float(result.speed_mb_s) if result.speed_mb_s is not None else None,
                    str(result.speed_unit),
                    float(result.loss_pct) if result.loss_pct is not None else None,
                    str(result.error_code),
                )
            )
    return rows, exits


def persist_run(
    db_path: str | Path,
    *,
    run_id: str,
    runner_id: str,
    profile: str,
    status: str,
    counts: Mapping[str, int],
    source_reports: Sequence[SourceReport],
    license_tags: Mapping[str, str],
    nodes: Sequence[ProxyNode],
    edges: Sequence[EdgeEndpoint],
    probe_results: Sequence[Any],
    window_days: int,
    now: datetime | None = None,
) -> PersistSummary:
    """Persist one run into the local SQLite history database (single transaction)."""
    moment = now or _now()
    stamp = dbmod.utc_stamp(moment)
    rows, exits = _observation_rows(run_id, runner_id, probe_results, moment)
    database = str(db_path)
    conn: sqlite3.Connection | None = None
    try:
        conn = dbmod.open_db(db_path)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            dbmod.UPSERT_RUN,
            (
                run_id,
                runner_id,
                profile,
                status,
                1,
                0,
                stamp,
                int(counts.get("sources_total", 0)),
                int(counts.get("sources_failed", 0)),
                int(counts.get("raw_items", 0)),
                int(counts.get("proxy_nodes", 0)),
                int(counts.get("edge_endpoints", 0)),
                int(counts.get("parse_issues", 0)),
            ),
        )
        for report in source_reports:
            conn.execute(
                dbmod.UPSERT_SOURCE,
                (
                    run_id,
                    report.source_id,
                    1 if report.ok else 0,
                    int(report.fetched),
                    report.scope,
                    str(license_tags.get(report.source_id, "unknown")),
                    ",".join(error.code for error in report.errors),
                ),
            )
        item_payloads: list[tuple[str, str, str, dict[str, Any]]] = []
        for node in nodes:
            item_payloads.append(
                (
                    node.item_id,
                    node.kind,
                    node.fingerprint,
                    node.model_dump(mode="json"),
                )
            )
        for edge in edges:
            item_payloads.append(
                (
                    edge.item_id,
                    edge.kind,
                    edge.fingerprint,
                    edge.model_dump(mode="json"),
                )
            )
        for item_id, kind, fingerprint, payload in item_payloads:
            conn.execute(
                dbmod.UPSERT_ITEM,
                (
                    item_id,
                    kind,
                    fingerprint,
                    run_id,
                    run_id,
                    stamp,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
            for source_id in _source_ids(payload):
                conn.execute(dbmod.UPSERT_SOURCE_ITEM, (run_id, source_id, item_id))
        conn.executemany(dbmod.UPSERT_OBSERVATION, rows)
        conn.executemany(dbmod.UPSERT_EXIT, exits)
        pruned = prune_runs(conn, window_days=window_days, now=moment)
        quality = source_quality(
            conn,
            runner_id=runner_id,
            window_days=window_days,
            exclude_run_id=run_id,
            now=moment,
        )
        conn.execute("COMMIT")
    except (sqlite3.Error, OSError) as err:
        if conn is not None:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise StorageError(
            code="storage_error",
            message=f"{type(err).__name__}: {err}",
            retryable=True,
        ) from err
    finally:
        if conn is not None:
            conn.close()
    return PersistSummary(
        status="ok",
        database=database,
        runs=1,
        sources=len(source_reports),
        items=len(item_payloads),
        observations=len(rows),
        pruned=pruned,
        source_quality=quality,
    )


def _source_ids(payload: Mapping[str, Any]) -> list[str]:
    values = payload.get("source_ids") or []
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if str(value).strip()]


def load_entities(
    db_path: str | Path, run_id: str
) -> tuple[list[ProxyNode], list[EdgeEndpoint]]:
    """Load persisted entities (including private material) for a run."""
    if not Path(db_path).is_file():
        raise StorageError(
            code="database_missing",
            message=f"history database not found: {db_path}",
        )
    conn = dbmod.open_db(db_path)
    try:
        run_row = conn.execute(
            "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run_row is None:
            raise StorageError(
                code="run_missing",
                message=f"run not found in history database: {run_id}",
            )
        rows = conn.execute(
            """
            SELECT i.payload FROM source_items AS si
            JOIN items AS i ON i.item_id = si.item_id
            WHERE si.run_id = ?
            ORDER BY i.item_id
            """,
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    nodes: list[ProxyNode] = []
    edges: list[EdgeEndpoint] = []
    for row in rows:
        payload = json.loads(str(row["payload"]))
        kind = str(payload.get("kind") or "")
        if kind == "proxy_node":
            nodes.append(ProxyNode.model_validate(payload))
        elif kind == "edge_endpoint":
            edges.append(EdgeEndpoint.model_validate(payload))
        else:
            raise StorageError(
                code="entity_kind_unknown",
                message=f"unknown persisted entity kind: {kind!r}",
            )
    return nodes, edges


def decode_probe_results(payload: Mapping[str, Any]) -> list[Any]:
    """Rebuild probe result models from a probe-results.json payload."""
    results: list[Any] = []
    for entry in payload.get("results") or []:
        if not isinstance(entry, Mapping):
            raise StorageError(
                code="probe_results_invalid",
                message="probe-results payload entries must be objects",
            )
        kind = str(entry.get("kind") or "")
        if kind == "proxy_probe_result":
            results.append(ProxyProbeResult.model_validate(dict(entry)))
        elif kind == "endpoint_probe_result":
            results.append(EndpointProbeResult.model_validate(dict(entry)))
        else:
            raise StorageError(
                code="probe_results_invalid",
                message=f"unknown probe result kind: {kind!r}",
            )
    return results


__all__ = [
    "PROXY_TEST_TYPE",
    "ENDPOINT_TEST_TYPE",
    "persist_run",
    "load_entities",
    "decode_probe_results",
]
