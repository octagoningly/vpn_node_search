from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_FILENAME = "nodebench.db"
DB_SCHEMA_VERSION = 1

DDL = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        runner_id TEXT NOT NULL,
        profile TEXT NOT NULL,
        status TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        dry_run INTEGER NOT NULL DEFAULT 0,
        generated_at TEXT NOT NULL,
        sources_total INTEGER NOT NULL DEFAULT 0,
        sources_failed INTEGER NOT NULL DEFAULT 0,
        raw_items INTEGER NOT NULL DEFAULT 0,
        proxy_nodes INTEGER NOT NULL DEFAULT 0,
        edge_endpoints INTEGER NOT NULL DEFAULT 0,
        parse_issues INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sources (
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        source_id TEXT NOT NULL,
        ok INTEGER NOT NULL,
        fetched INTEGER NOT NULL DEFAULT 0,
        scope TEXT NOT NULL DEFAULT '',
        license_tag TEXT NOT NULL DEFAULT 'unknown',
        error_codes TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (run_id, source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS items (
        item_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        first_seen_run TEXT NOT NULL,
        last_seen_run TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_items (
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        source_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        PRIMARY KEY (run_id, source_id, item_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS probe_observations (
        item_id TEXT NOT NULL,
        runner_id TEXT NOT NULL,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        test_type TEXT NOT NULL,
        measured_at TEXT NOT NULL,
        status TEXT NOT NULL,
        probe_mode TEXT NOT NULL DEFAULT '',
        backend TEXT NOT NULL DEFAULT '',
        skipped_reason TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        timeouts INTEGER NOT NULL DEFAULT 0,
        latency_ms REAL,
        speed_mb_s REAL,
        speed_unit TEXT NOT NULL DEFAULT 'MB/s',
        loss_pct REAL,
        error_code TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (item_id, runner_id, run_id, test_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS exit_observations (
        item_id TEXT NOT NULL,
        runner_id TEXT NOT NULL,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        test_type TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        exit_ip TEXT NOT NULL DEFAULT '',
        country_code TEXT,
        asn TEXT,
        isp TEXT,
        PRIMARY KEY (item_id, runner_id, run_id, test_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reputation_observations (
        item_id TEXT NOT NULL,
        runner_id TEXT NOT NULL,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        test_type TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT '',
        raw_score REAL,
        risk_level REAL,
        evidence TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (item_id, runner_id, run_id, test_type)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_probe_runner_time
        ON probe_observations (runner_id, measured_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_source_items_item
        ON source_items (item_id)
    """,
)

UPSERT_RUN = """
    INSERT INTO runs (
        run_id, runner_id, profile, status, schema_version, dry_run,
        generated_at, sources_total, sources_failed, raw_items,
        proxy_nodes, edge_endpoints, parse_issues
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(run_id) DO UPDATE SET
        runner_id=excluded.runner_id,
        profile=excluded.profile,
        status=excluded.status,
        schema_version=excluded.schema_version,
        dry_run=excluded.dry_run,
        generated_at=excluded.generated_at,
        sources_total=excluded.sources_total,
        sources_failed=excluded.sources_failed,
        raw_items=excluded.raw_items,
        proxy_nodes=excluded.proxy_nodes,
        edge_endpoints=excluded.edge_endpoints,
        parse_issues=excluded.parse_issues
"""

UPSERT_SOURCE = """
    INSERT INTO sources (
        run_id, source_id, ok, fetched, scope, license_tag, error_codes
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(run_id, source_id) DO UPDATE SET
        ok=excluded.ok,
        fetched=excluded.fetched,
        scope=excluded.scope,
        license_tag=excluded.license_tag,
        error_codes=excluded.error_codes
"""

UPSERT_ITEM = """
    INSERT INTO items (
        item_id, kind, fingerprint, first_seen_run, last_seen_run,
        last_seen_at, payload
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(item_id) DO UPDATE SET
        kind=excluded.kind,
        fingerprint=excluded.fingerprint,
        last_seen_run=excluded.last_seen_run,
        last_seen_at=excluded.last_seen_at,
        payload=excluded.payload
"""

UPSERT_SOURCE_ITEM = """
    INSERT OR IGNORE INTO source_items (run_id, source_id, item_id)
    VALUES (?, ?, ?)
"""

UPSERT_OBSERVATION = """
    INSERT INTO probe_observations (
        item_id, runner_id, run_id, test_type, measured_at, status,
        probe_mode, backend, skipped_reason, attempts, timeouts,
        latency_ms, speed_mb_s, speed_unit, loss_pct, error_code
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(item_id, runner_id, run_id, test_type) DO UPDATE SET
        measured_at=excluded.measured_at,
        status=excluded.status,
        probe_mode=excluded.probe_mode,
        backend=excluded.backend,
        skipped_reason=excluded.skipped_reason,
        attempts=excluded.attempts,
        timeouts=excluded.timeouts,
        latency_ms=excluded.latency_ms,
        speed_mb_s=excluded.speed_mb_s,
        speed_unit=excluded.speed_unit,
        loss_pct=excluded.loss_pct,
        error_code=excluded.error_code
"""

UPSERT_EXIT = """
    INSERT INTO exit_observations (
        item_id, runner_id, run_id, test_type, observed_at,
        exit_ip, country_code, asn, isp
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(item_id, runner_id, run_id, test_type) DO UPDATE SET
        observed_at=excluded.observed_at,
        exit_ip=excluded.exit_ip,
        country_code=excluded.country_code,
        asn=excluded.asn,
        isp=excluded.isp
"""


def utc_stamp(moment: datetime | None = None) -> str:
    value = moment or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    text = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return text


def open_db(path: str | Path) -> sqlite3.Connection:
    """Open (creating when needed) the local history database."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    for statement in DDL:
        conn.execute(statement)
    row = conn.execute(
        "SELECT version FROM schema_migrations WHERE version = ?", (DB_SCHEMA_VERSION,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (DB_SCHEMA_VERSION, utc_stamp()),
        )


__all__ = [
    "DB_FILENAME",
    "DB_SCHEMA_VERSION",
    "DDL",
    "UPSERT_RUN",
    "UPSERT_SOURCE",
    "UPSERT_ITEM",
    "UPSERT_SOURCE_ITEM",
    "UPSERT_OBSERVATION",
    "UPSERT_EXIT",
    "open_db",
    "utc_stamp",
]
