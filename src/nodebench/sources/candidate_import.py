from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nodebench.core.config import AppConfig, CandidateImportConfig
from nodebench.core.schema import ErrorInfo, RawItem, SourceReport
from nodebench.parsers.csv import HEADER_FULL
from nodebench.sources.base import BOM, make_error, resolve_base_dir

SOURCE_ID = "imported"
MAX_IMPORT_FILE_BYTES = 2 * 1024 * 1024

PROBLEM_MESSAGES = {
    "empty_file": "import file has no content: {display}",
    "no_candidates": "no usable candidates in import file: {display}",
}


def _display_path(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.name


def _read_text(path: Path, display: str, errors: list[ErrorInfo]) -> str | None:
    try:
        is_file = path.is_file()
        exists = path.exists()
    except PermissionError:
        errors.append(
            make_error("file_permission", f"import file permission denied: {display}", True)
        )
        return None
    except OSError:
        errors.append(
            make_error("file_unreadable", f"import file cannot be read: {display}", True)
        )
        return None
    if not is_file:
        if exists:
            errors.append(
                make_error("file_invalid", f"import path is not a file: {display}")
            )
        else:
            errors.append(
                make_error("file_missing", f"import file does not exist: {display}")
            )
        return None
    try:
        size = path.stat().st_size
    except PermissionError:
        errors.append(
            make_error("file_permission", f"import file permission denied: {display}", True)
        )
        return None
    except OSError:
        errors.append(
            make_error("file_unreadable", f"import file cannot be read: {display}", True)
        )
        return None
    if size > MAX_IMPORT_FILE_BYTES:
        errors.append(
            make_error(
                "file_too_large",
                f"import file exceeds {MAX_IMPORT_FILE_BYTES} bytes: {display}",
            )
        )
        return None
    try:
        data = path.read_bytes()
    except PermissionError:
        errors.append(
            make_error("file_permission", f"import file permission denied: {display}", True)
        )
        return None
    except OSError:
        errors.append(
            make_error("file_unreadable", f"import file cannot be read: {display}", True)
        )
        return None
    try:
        return data.decode("utf-8").removeprefix(BOM)
    except UnicodeDecodeError:
        errors.append(
            make_error("decode_failed", f"import file is not valid utf-8: {display}")
        )
        return None


def _first_data_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def detect_import_format(text: str, path: Path) -> str:
    """Return ``csv`` for ADDCSV / iptest nine-column files else ``endpoint_list``."""
    first = _first_data_line(text)
    cells = [cell.strip() for cell in first.split(",")]
    if cells == list(HEADER_FULL):
        return "csv"
    if path.suffix.lower() == ".csv" and len(cells) >= 2:
        return "csv"
    return "endpoint_list"


def _usable_lines(text: str) -> int:
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        count += 1
    return count


def collect_candidate_import(
    cfg: AppConfig,
    ctx: Any = None,
    *,
    base_dir: str | Path | None = None,
) -> tuple[list[RawItem], list[SourceReport]]:
    """Import operator-supplied ADDAPI/ADDCSV lists as CF-style candidates.

    Rows keep their remarks and any CSV latency/speed as historical
    reference in item params after parsing; they are never promoted to
    this-run measurements.
    """
    source: CandidateImportConfig = cfg.sources.candidate_import
    if not source.enabled:
        return [], []
    if not source.files:
        return [], [
            SourceReport(
                source_id=SOURCE_ID,
                ok=False,
                errors=[
                    make_error(
                        "no_files",
                        "candidate_import is enabled but files is empty",
                    )
                ],
            )
        ]
    base = resolve_base_dir(ctx, base_dir)
    limit = max(int(source.max_ips_per_run), 0)
    license_tag = str(source.license_tag or "user_supplied") or "user_supplied"
    resolved: list[tuple[Path, str]] = []
    for raw in source.files:
        path = Path(raw)
        if not path.is_absolute():
            path = base / path
        resolved.append((path, _display_path(path, base)))
    scope = ", ".join(display for _, display in resolved)
    errors: list[ErrorInfo] = []
    items: list[RawItem] = []
    budget = limit
    truncated = False
    fetched_at = datetime.now(timezone.utc)
    for path, display in resolved:
        text = _read_text(path, display, errors)
        if text is None:
            continue
        if not text.strip():
            errors.append(make_error("empty_file", PROBLEM_MESSAGES["empty_file"].format(display=display)))
            continue
        content_type = detect_import_format(text, path)
        if content_type == "csv":
            # header + data rows; header does not count against budget
            data_rows = max(_usable_lines(text) - 1, 0)
        else:
            data_rows = _usable_lines(text)
        if data_rows <= 0:
            errors.append(
                make_error("no_candidates", PROBLEM_MESSAGES["no_candidates"].format(display=display))
            )
            continue
        take = data_rows if budget >= data_rows else budget
        if take < data_rows:
            truncated = True
        if take <= 0:
            truncated = True
            break
        if content_type == "csv":
            lines = text.splitlines()
            header = lines[0] if lines else ""
            body = [line for line in lines[1:] if line.strip()]
            payload = "\n".join([header, *body[:take]]) + "\n"
        else:
            body = [line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
            payload = "\n".join(body[:take]) + "\n"
        items.append(
            RawItem(
                source_id=SOURCE_ID,
                content_type=content_type,  # type: ignore[arg-type]
                payload=payload,
                fetched_at=fetched_at,
                license_tag=license_tag,
                source_ref=display,
            )
        )
        budget -= take
        if truncated:
            errors.append(
                make_error(
                    "ip_limit_exceeded",
                    f"candidate import limit {limit} reached; remaining candidates truncated at {display}",
                )
            )
            break
    ok = bool(items) and all(error.code == "ip_limit_exceeded" for error in errors)
    report = SourceReport(
        source_id=SOURCE_ID,
        ok=ok,
        fetched=len(items),
        errors=errors,
        scope=scope,
    )
    return items, [report]


__all__ = [
    "SOURCE_ID",
    "MAX_IMPORT_FILE_BYTES",
    "collect_candidate_import",
    "detect_import_format",
]
