from __future__ import annotations

from typing import Any, Iterable, Mapping

from nodebench.core.schema import ExportedFile

MANIFEST_NAME = "manifest.json"

_MANIFEST_KEYS = ("name", "sha256", "size_bytes", "entry_count")


def _entry(item: Any) -> dict[str, Any]:
    if isinstance(item, ExportedFile):
        return {"name": item.name, "sha256": item.sha256, "size_bytes": item.size_bytes, "entry_count": item.entry_count}
    if isinstance(item, Mapping):
        return {key: item[key] for key in _MANIFEST_KEYS}
    return {
        "name": item.name,
        "sha256": item.sha256,
        "size_bytes": item.size_bytes,
        "entry_count": item.entry_count,
    }


def build_manifest(
    *,
    schema_version: int,
    run_id: str,
    runner_id: str,
    profile: str,
    generated_at: Any,
    status: str,
    scoring_version: str,
    validation: Any,
    publishable: bool,
    files: Iterable[Any],
    counts: Mapping[str, int],
) -> dict[str, Any]:
    entries = sorted((_entry(item) for item in files), key=lambda entry: entry["name"])
    return {
        "schema_version": int(schema_version),
        "run_id": run_id,
        "runner_id": runner_id,
        "profile": profile,
        "generated_at": generated_at,
        "status": status,
        "scoring_version": scoring_version,
        "validation": validation,
        "publishable": publishable,
        "files": entries,
        "counts": dict(counts),
    }
