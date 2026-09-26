from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from nodebench.core.config import LocalSourceConfig
from nodebench.core.schema import ErrorInfo, RawItem, SourceReport
from nodebench.sources.base import (
    BOM,
    MAX_SOURCE_FILES,
    MAX_SOURCE_FILE_BYTES,
    CollectOutcome,
    content_type_for,
    make_error,
)

SOURCE_ID = "local"
LICENSE_TAG = "unknown"


def _display_path(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _is_hidden(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = Path(path.name)
    return any(part.startswith(".") for part in relative.parts)


def _iter_candidates(
    paths: list[str], base: Path, errors: list[ErrorInfo]
) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    truncated = False
    for raw in paths:
        if truncated:
            break
        path = Path(raw)
        if not path.is_absolute():
            path = base / path
        if not path.exists():
            errors.append(
                make_error("path_missing", f"configured path does not exist: {raw}")
            )
            continue
        if path.is_dir():
            children = sorted(
                child
                for child in path.rglob("*")
                if child.is_file() and not _is_hidden(child, path)
            )
        elif path.is_file():
            children = [path]
        else:
            errors.append(
                make_error(
                    "path_invalid",
                    f"configured path is not a file or directory: {raw}",
                )
            )
            continue
        for child in children:
            key = child.resolve()
            if key in seen:
                continue
            if len(candidates) >= MAX_SOURCE_FILES:
                truncated = True
                break
            seen.add(key)
            candidates.append(child)
    if truncated:
        errors.append(
            make_error(
                "file_limit_exceeded",
                f"file limit {MAX_SOURCE_FILES} reached; remaining files skipped",
            )
        )
    return candidates


def _read_item(
    candidate: Path,
    base: Path,
    fetched_at: datetime,
    errors: list[ErrorInfo],
) -> RawItem | None:
    display = _display_path(candidate, base)
    try:
        size = candidate.stat().st_size
    except OSError:
        errors.append(
            make_error("file_unreadable", f"file cannot be read: {display}", True)
        )
        return None
    if size > MAX_SOURCE_FILE_BYTES:
        errors.append(
            make_error(
                "file_too_large",
                f"file exceeds {MAX_SOURCE_FILE_BYTES} bytes: {display}",
            )
        )
        return None
    try:
        data = candidate.read_bytes()
    except OSError:
        errors.append(
            make_error("file_unreadable", f"file cannot be read: {display}", True)
        )
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        errors.append(
            make_error("decode_failed", f"file is not valid utf-8: {display}")
        )
        return None
    text = text.removeprefix(BOM)
    if not text.strip():
        errors.append(make_error("empty_file", f"file has no content: {display}"))
        return None
    return RawItem(
        source_id=SOURCE_ID,
        content_type=content_type_for(candidate, text),
        payload=text,
        fetched_at=fetched_at,
        license_tag=LICENSE_TAG,
        source_ref=display,
    )


def collect_local(
    source: LocalSourceConfig, base_dir: Path | None = None
) -> CollectOutcome:
    if not source.enabled:
        return CollectOutcome()
    base = Path(base_dir) if base_dir is not None else Path.cwd()
    if not source.paths:
        return CollectOutcome(
            reports=[
                SourceReport(
                    source_id=SOURCE_ID,
                    ok=False,
                    errors=[
                        make_error(
                            "no_paths",
                            "local source is enabled but no paths are configured",
                        )
                    ],
                )
            ]
        )
    errors: list[ErrorInfo] = []
    candidates = _iter_candidates(source.paths, base, errors)
    if not candidates and not errors:
        errors.append(
            make_error(
                "no_files_found", "configured local paths contain no readable files"
            )
        )
    items: list[RawItem] = []
    fetched_at = datetime.now(timezone.utc)
    for candidate in candidates:
        item = _read_item(candidate, base, fetched_at, errors)
        if item is not None:
            items.append(item)
    report = SourceReport(
        source_id=SOURCE_ID,
        ok=bool(items),
        fetched=len(items),
        errors=errors,
        scope=", ".join(source.paths),
    )
    return CollectOutcome(items=items, reports=[report])


__all__ = ["SOURCE_ID", "LICENSE_TAG", "collect_local"]
