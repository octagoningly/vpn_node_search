from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from nodebench.core.schema import HistorySummary, SourceQuality
from nodebench.history.db import utc_stamp

SAMPLE_STATUS = "ok"


def _parse_stamp(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def _cutoff(now: datetime, days: int) -> str:
    return utc_stamp(now - timedelta(days=days))


def prune_runs(conn: sqlite3.Connection, *, window_days: int, now: datetime) -> int:
    """Delete runs older than the history window (cascade cleans observations)."""
    if window_days < 1:
        raise ValueError("window_days must be at least 1")
    cursor = conn.execute("DELETE FROM runs WHERE generated_at < ?", (_cutoff(now, window_days),))
    return max(int(cursor.rowcount or 0), 0)


def source_quality(
    conn: sqlite3.Connection,
    *,
    runner_id: str,
    window_days: int,
    exclude_run_id: str,
    now: datetime,
) -> list[SourceQuality]:
    """Aggregate per-source success over the history window (current run excluded)."""
    rows = conn.execute(
        """
        SELECT s.source_id, s.ok, s.fetched, s.error_codes
        FROM sources AS s
        JOIN runs AS r ON r.run_id = s.run_id
        WHERE r.runner_id = ? AND r.generated_at >= ? AND r.run_id != ?
        ORDER BY s.source_id
        """,
        (runner_id, _cutoff(now, window_days), exclude_run_id),
    ).fetchall()
    totals: dict[str, SourceQuality] = {}
    for row in rows:
        entry = totals.setdefault(
            str(row["source_id"]),
            SourceQuality(source_id=str(row["source_id"])),
        )
        entry.runs_seen += 1
        entry.ok_runs += 1 if int(row["ok"]) else 0
        entry.fetched_total += int(row["fetched"])
        codes = [code for code in str(row["error_codes"] or "").split(",") if code]
        entry.error_total += len(codes)
    return sorted(totals.values(), key=lambda entry: entry.source_id)


def _scheduled_pairs(
    conn: sqlite3.Connection,
    *,
    runner_id: str,
    exclude_run_id: str,
    earliest_cutoff: str,
) -> tuple[dict[str, str], set[tuple[str, str]]]:
    """Run generation times and (run_id, item_id) pairs scheduled inside the window."""
    run_rows = conn.execute(
        """
        SELECT run_id, generated_at FROM runs
        WHERE runner_id = ? AND run_id != ? AND generated_at >= ?
        """,
        (runner_id, exclude_run_id, earliest_cutoff),
    ).fetchall()
    run_times = {str(row["run_id"]): str(row["generated_at"]) for row in run_rows}
    if not run_times:
        return run_times, set()
    pairs: set[tuple[str, str]] = set()
    for run_id in run_times:
        rows = conn.execute(
            "SELECT item_id FROM source_items WHERE run_id = ?", (run_id,)
        ).fetchall()
        for row in rows:
            pairs.add((run_id, str(row["item_id"])))
    return run_times, pairs


def history_for_items(
    conn: sqlite3.Connection,
    *,
    test_type: str,
    runner_id: str,
    exclude_run_id: str,
    windows: list[int],
    min_samples: int,
    now: datetime,
) -> dict[str, dict[int, HistorySummary]]:
    """Per-item history summaries keyed by item_id then window length in days."""
    cleaned = sorted({int(days) for days in windows})
    if not cleaned or any(days < 1 for days in cleaned):
        raise ValueError("history windows must be positive day counts")
    cutoffs = {days: _cutoff(now, days) for days in cleaned}
    earliest = min(cutoffs.values())
    run_times, scheduled_pairs = _scheduled_pairs(
        conn,
        runner_id=runner_id,
        exclude_run_id=exclude_run_id,
        earliest_cutoff=earliest,
    )
    obs_rows = conn.execute(
        """
        SELECT item_id, run_id, status, measured_at
        FROM probe_observations
        WHERE runner_id = ? AND run_id != ? AND test_type = ? AND measured_at >= ?
        """,
        (runner_id, exclude_run_id, test_type, earliest),
    ).fetchall()
    counted: dict[str, list[tuple[str, str, str]]] = {}
    for row in obs_rows:
        pair = (str(row["run_id"]), str(row["item_id"]))
        if pair not in scheduled_pairs:
            continue
        status = str(row["status"])
        if status == "skipped":
            continue
        counted.setdefault(str(row["item_id"]), []).append(
            (str(row["run_id"]), status, str(row["measured_at"]))
        )
    scheduled_counts: dict[str, dict[int, int]] = {}
    for run_id, item_id in scheduled_pairs:
        generated = run_times.get(run_id)
        if generated is None:
            continue
        item_counts = scheduled_counts.setdefault(item_id, {days: 0 for days in cleaned})
        for days in cleaned:
            if generated >= cutoffs[days]:
                item_counts[days] += 1
    summaries: dict[str, dict[int, HistorySummary]] = {}
    for item_id in sorted(scheduled_counts.keys() | counted.keys()):
        rows = counted.get(item_id, [])
        window_summaries: dict[int, HistorySummary] = {}
        for days in cleaned:
            cutoff = cutoffs[days]
            in_window = [row for row in rows if row[2] >= cutoff]
            scheduled = scheduled_counts.get(item_id, {}).get(days, 0)
            executed = len(in_window)
            succeeded = sum(
                1 for _run_id, status, _stamp in in_window if status == SAMPLE_STATUS
            )
            if scheduled < executed:
                scheduled = executed
            availability = (succeeded / executed) if executed else None
            stamps = sorted(row[2] for row in in_window)
            window_summaries[days] = HistorySummary(
                item_id=item_id,
                window_days=days,
                scheduled=scheduled,
                executed=executed,
                succeeded=succeeded,
                sample_count=succeeded,
                availability_rate=availability,
                first_observed_at=_parse_stamp(stamps[0]) if stamps else None,
                last_observed_at=_parse_stamp(stamps[-1]) if stamps else None,
                min_samples=min_samples,
            )
        summaries[item_id] = window_summaries
    return summaries


__all__ = [
    "prune_runs",
    "source_quality",
    "history_for_items",
]
