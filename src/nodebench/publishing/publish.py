from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from nodebench.core.schema import ExportOutcome, PublishResult
from nodebench.core.serialization import public_dump, write_json_atomic
from nodebench.exporters.cf_addapi import CF_ADDAPI_NAME
from nodebench.exporters.cf_addcsv import CF_ADDCSV_NAME
from nodebench.exporters.clash import PROXY_CLASH_NAME
from nodebench.exporters.manifest import MANIFEST_NAME
from nodebench.exporters.raw import PROXY_RAW_NAME
from nodebench.exporters.report import REPORT_NAME, report_bytes
from nodebench.probes import CAPABILITY_SKIP_REASONS

REDISTRIBUTIBLE_LICENSE_TAGS = frozenset(
    {
        "apache-2.0",
        "bsd-2-clause",
        "bsd-3-clause",
        "cc-by-4.0",
        "cc-by-sa-4.0",
        "cc0-1.0",
        "isc",
        "mit",
        "mpl-2.0",
        "odbl",
        "public-domain",
    }
)

PROXY_CONTENT_FILES = (PROXY_CLASH_NAME, PROXY_RAW_NAME)
ENDPOINT_CONTENT_FILES = (CF_ADDAPI_NAME, CF_ADDCSV_NAME)


def is_redistributable(tag: str) -> bool:
    return str(tag or "").strip().lower() in REDISTRIBUTIBLE_LICENSE_TAGS


def _licenses_ok(tags: Iterable[str]) -> bool:
    collected = {str(tag or "").strip().lower() for tag in tags}
    return bool(collected) and all(is_redistributable(tag) for tag in collected)


def _entry_count(outcome: ExportOutcome, name: str) -> int:
    for entry in outcome.files:
        if entry.name == name:
            return int(entry.entry_count)
    return 0


def _probe_degraded(report: Mapping[str, Any]) -> bool:
    probe = report.get("probe") or {}
    counts = report.get("counts") or {}
    entities = {
        "proxy": int(counts.get("proxy_nodes") or 0),
        "cf": int(counts.get("edge_endpoints") or 0),
    }
    for kind, node in probe.items():
        if not isinstance(node, Mapping):
            continue
        if int(entities.get(kind, 0)) <= 0:
            continue
        if node.get("mode") == "skip" and str(node.get("skipped_reason") or "") in CAPABILITY_SKIP_REASONS:
            return True
    return False


def _gate(
    *,
    report: Mapping[str, Any],
    outcome: ExportOutcome,
    ranked_proxies: int,
    ranked_endpoints: int,
    proxy_license_ok: bool,
    proxy_content: int,
) -> list[str]:
    blocked: list[str] = []
    if outcome.status != "ok":
        blocked.append("export_failed")
    if outcome.validation is None or not outcome.validation.ok:
        blocked.append("validation_failed")
    if ranked_proxies <= 0 and ranked_endpoints <= 0:
        blocked.append("zero_valid_items")
    if _probe_degraded(report):
        blocked.append("probe_incomplete")
    counts = report.get("counts") or {}
    total = int(counts.get("sources_total") or 0)
    failed = int(counts.get("sources_failed") or 0)
    if total > 0 and failed == total:
        blocked.append("critical_source_failure")
    if proxy_content > 0 and not proxy_license_ok:
        blocked.append("license_not_redistributable")
    if not outcome.publishable:
        blocked.append("nothing_publishable")
    return blocked


def publish_output(
    *,
    export_dir: str | Path,
    target_dir: str | Path,
    report: Mapping[str, Any],
    outcome: ExportOutcome,
    ranked_proxies: int,
    ranked_endpoints: int,
    proxy_license_tags: Iterable[str],
    endpoint_license_tags: Iterable[str],
    enabled: bool = True,
    allow_publish: bool = True,
    allow_proxy_credentials: bool = False,
) -> PublishResult:
    if not enabled:
        return PublishResult(status="skipped", reason="disabled")
    if not allow_publish:
        return PublishResult(status="skipped", reason="no_publish_flag")

    proxy_license_ok = _licenses_ok(proxy_license_tags)
    endpoint_license_ok = _licenses_ok(endpoint_license_tags)
    proxy_content = _entry_count(outcome, PROXY_RAW_NAME)

    excluded: list[str] = []
    for name in PROXY_CONTENT_FILES:
        if _entry_count(outcome, name) <= 0 or not proxy_license_ok:
            excluded.append(name)
    for name in ENDPOINT_CONTENT_FILES:
        if _entry_count(outcome, name) <= 0 or not endpoint_license_ok:
            excluded.append(name)

    blocked = _gate(
        report=report,
        outcome=outcome,
        ranked_proxies=ranked_proxies,
        ranked_endpoints=ranked_endpoints,
        proxy_license_ok=proxy_license_ok,
        proxy_content=proxy_content,
    )
    if blocked:
        return PublishResult(
            status="blocked",
            reason=",".join(blocked),
            blocked=blocked,
            excluded=sorted(excluded),
        )

    published = sorted(
        entry.name for entry in outcome.files if entry.name not in set(excluded)
    )
    source = Path(export_dir)
    target = Path(target_dir)
    run_id = str(report.get("run_id") or "run")
    staging = target.parent / ("." + target.name + ".tmp-" + run_id)
    previous = target.parent / ("." + target.name + ".old")
    replaced_previous = target.exists()
    preview = PublishResult(
        status="ok",
        reason="",
        path=str(target),
        files=list(published),
        blocked=[],
        errors=[],
        excluded=sorted(excluded),
        replaced_previous=replaced_previous,
    )

    try:
        _materialize(
            source,
            staging,
            previous,
            target,
            report,
            preview,
        )
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        return PublishResult(
            status="failed",
            reason="publish_failed",
            blocked=blocked,
            excluded=sorted(excluded),
            errors=["publish_failed"],
            replaced_previous=replaced_previous,
        )
    return preview


def _materialize(
    source: Path,
    staging: Path,
    previous: Path,
    target: Path,
    report: Mapping[str, Any],
    preview: PublishResult,
) -> None:
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for name in preview.files:
        shutil.copy2(source / name, staging / name)

    updated = dict(report)
    stages = dict(updated.get("stages") or {})
    stages["publish"] = public_dump(preview)
    updated["stages"] = stages
    pending = updated.get("stages_pending")
    if isinstance(pending, list):
        updated["stages_pending"] = [item for item in pending if item != "publish"]
    write_json_atomic(staging / REPORT_NAME, updated)
    _relink_manifest(staging, updated)

    shutil.rmtree(previous, ignore_errors=True)
    if target.exists():
        target.rename(previous)
    try:
        staging.rename(target)
    except OSError:
        if not target.exists() and previous.exists():
            previous.rename(target)
        raise
    shutil.rmtree(previous, ignore_errors=True)


def _relink_manifest(staging: Path, report: Mapping[str, Any]) -> None:
    payload = report_bytes(report)
    manifest_path = staging / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("files")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name") == REPORT_NAME:
                entry["sha256"] = hashlib.sha256(payload).hexdigest()
                entry["size_bytes"] = len(payload)
    write_json_atomic(manifest_path, manifest)
