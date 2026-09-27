"""Publishing gates that turn a private export into the public directory."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from nodebench.core.schema import (
    PUBLIC_VISIBILITY,
    USER_SUPPLIED_LICENSE_TAGS,
    ExportOutcome,
    PublishResult,
)
from nodebench.core.serialization import public_dump, write_json_atomic
from nodebench.exporters.cf_addapi import CF_ADDAPI_NAME
from nodebench.exporters.cf_addcsv import CF_ADDCSV_NAME
from nodebench.exporters.clash import PROXY_CLASH_NAME
from nodebench.exporters.manifest import MANIFEST_NAME
from nodebench.exporters.raw import PROXY_RAW_NAME
from nodebench.exporters.report import REPORT_NAME
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

REASON_EMPTY = "no_content"
REASON_LICENSE = "license_not_redistributable"
REASON_PROXY_CREDENTIALS = "proxy_credentials_not_allowed"
REASON_CF_NOT_AUTHORIZED = "cf_candidates_not_authorized"


def is_redistributable(tag: str) -> bool:
    """Return True when a license tag allows redistribution."""
    return str(tag or "").strip().lower() in REDISTRIBUTIBLE_LICENSE_TAGS


def _normalize(tags: Iterable[str]) -> set[str]:
    return {str(tag or "").strip().lower() for tag in tags}


def _licenses_ok(tags: Iterable[str]) -> bool:
    collected = _normalize(tags)
    return bool(collected) and all(is_redistributable(tag) for tag in collected)


def _endpoint_gate(tags: Iterable[str], cf_candidates_authorized: bool) -> tuple[bool, str]:
    """Decide whether endpoint files may enter the public directory.

    User imported CF candidates are released by the explicit
    ``cf_candidates_authorized`` flag instead of a license tag, while
    ordinary endpoint sources keep the license gate.
    """
    collected = _normalize(tags)
    if collected & USER_SUPPLIED_LICENSE_TAGS:
        if not cf_candidates_authorized:
            return False, REASON_CF_NOT_AUTHORIZED
        ordinary = collected - USER_SUPPLIED_LICENSE_TAGS
        if ordinary and not all(is_redistributable(tag) for tag in ordinary):
            return False, REASON_LICENSE
        return True, ""
    if not _licenses_ok(collected):
        return False, REASON_LICENSE
    return True, ""


def _entry_count(outcome: ExportOutcome, name: str) -> int:
    for entry in outcome.files:
        if entry.name == name:
            return int(entry.entry_count)
    return 0


def _exclusions(
    *,
    outcome: ExportOutcome,
    proxy_license_ok: bool,
    allow_proxy_credentials: bool,
    endpoint_ok: bool,
    endpoint_reason: str,
) -> tuple[list[str], dict[str, str]]:
    """Collect the files that must stay out of the public directory."""
    excluded: list[str] = []
    reasons: dict[str, str] = {}
    for name in PROXY_CONTENT_FILES:
        if _entry_count(outcome, name) <= 0:
            excluded.append(name)
            reasons[name] = REASON_EMPTY
        elif not proxy_license_ok:
            excluded.append(name)
            reasons[name] = REASON_LICENSE
        elif not allow_proxy_credentials:
            excluded.append(name)
            reasons[name] = REASON_PROXY_CREDENTIALS
    for name in ENDPOINT_CONTENT_FILES:
        if _entry_count(outcome, name) <= 0:
            excluded.append(name)
            reasons[name] = REASON_EMPTY
        elif not endpoint_ok:
            excluded.append(name)
            reasons[name] = endpoint_reason
    return excluded, reasons


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
    cf_candidates_authorized: bool = False,
) -> PublishResult:
    """Copy gate-approved files from the private export to the public directory.

    The source ``export_dir`` stays complete and private; only the target
    directory receives files that passed the proxy credential, license and
    user supplied CF candidate gates. The target manifest is regenerated so
    it describes exactly what landed on disk.
    """
    if not enabled:
        return PublishResult(status="skipped", reason="disabled")
    if not allow_publish:
        return PublishResult(status="skipped", reason="no_publish_flag")

    proxy_license_ok = _licenses_ok(proxy_license_tags)
    endpoint_tags = _normalize(endpoint_license_tags)
    cf_user_supplied = bool(endpoint_tags & USER_SUPPLIED_LICENSE_TAGS)
    endpoint_ok, endpoint_reason = _endpoint_gate(
        endpoint_tags, cf_candidates_authorized
    )
    proxy_content = _entry_count(outcome, PROXY_RAW_NAME)

    excluded, reasons = _exclusions(
        outcome=outcome,
        proxy_license_ok=proxy_license_ok,
        allow_proxy_credentials=allow_proxy_credentials,
        endpoint_ok=endpoint_ok,
        endpoint_reason=endpoint_reason,
    )
    cf_state = {
        "cf_candidates_user_supplied": cf_user_supplied,
        "cf_candidates_authorized": bool(cf_candidates_authorized),
    }

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
            visibility=PUBLIC_VISIBILITY,
            blocked=blocked,
            excluded=sorted(excluded),
            excluded_reasons=reasons,
            **cf_state,
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
        visibility=PUBLIC_VISIBILITY,
        path=str(target),
        files=list(published),
        blocked=[],
        errors=[],
        excluded=sorted(excluded),
        excluded_reasons=reasons,
        replaced_previous=replaced_previous,
        **cf_state,
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
            visibility=PUBLIC_VISIBILITY,
            blocked=blocked,
            excluded=sorted(excluded),
            excluded_reasons=reasons,
            errors=["publish_failed"],
            replaced_previous=replaced_previous,
            **cf_state,
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
    """Stage the public directory and swap it in atomically."""
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
    _relink_manifest(
        staging,
        cf_candidates_user_supplied=preview.cf_candidates_user_supplied,
        cf_candidates_authorized=preview.cf_candidates_authorized,
    )

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


def _relink_manifest(
    staging: Path,
    *,
    cf_candidates_user_supplied: bool,
    cf_candidates_authorized: bool,
) -> None:
    """Regenerate the public manifest from the files actually on disk.

    Only files present in the staging directory are listed, every digest and
    size comes from the real bytes, and the CF candidate fields record
    whether user supplied candidates were published under explicit
    authorization.
    """
    manifest_path = staging / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    previous: dict[str, Mapping[str, Any]] = {}
    entries = manifest.get("files")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
                previous[str(entry["name"])] = entry

    rebuilt: list[dict[str, Any]] = []
    total = 0
    for path in sorted(staging.iterdir()):
        if not path.is_file():
            continue
        total += 1
        if path.name == MANIFEST_NAME:
            continue
        payload = path.read_bytes()
        prior = previous.get(path.name) or {}
        rebuilt.append(
            {
                "name": path.name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
                "entry_count": int(prior.get("entry_count") or 0),
            }
        )

    counts = dict(manifest.get("counts") or {})
    counts["files"] = total
    manifest["files"] = rebuilt
    manifest["counts"] = counts
    manifest["publishable"] = bool(rebuilt)
    manifest["cf_candidates_user_supplied"] = bool(cf_candidates_user_supplied)
    manifest["cf_candidates_authorized"] = bool(cf_candidates_authorized)
    write_json_atomic(manifest_path, manifest)
