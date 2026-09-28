from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from nodebench.core.schema import (
    PRIVATE_VISIBILITY,
    EdgeEndpoint,
    ExportOutcome,
    ExportedFile,
    ProxyNode,
    RankedEndpoint,
    RankedProxy,
    USER_SUPPLIED_LICENSE_TAGS,
    ValidationReport,
)
from nodebench.core.serialization import dumps_json, public_dump, write_json_atomic
from nodebench.exporters.cf_addapi import (
    CF_ADDAPI_NAME,
    DEFAULT_ADDAPI_REMARK_TEMPLATE,
    addapi_remark_from_ranked,
    build_addapi,
)
from nodebench.exporters.cf_addcsv import CF_ADDCSV_NAME, build_addcsv
from nodebench.exporters.consumer_hints import build_consumer_hints
from nodebench.exporters.clash import PROXY_CLASH_NAME, build_clash_config, build_clash_proxy
from nodebench.exporters.manifest import MANIFEST_NAME, build_manifest
from nodebench.exporters.raw import PROXY_RAW_NAME, build_raw_text
from nodebench.exporters.report import REPORT_NAME, report_bytes
from nodebench.exporters.uri import build_proxy_uri
from nodebench.exporters.validate import scan_text

CONTENT_NAMES = (
    PROXY_CLASH_NAME,
    PROXY_RAW_NAME,
    CF_ADDAPI_NAME,
    CF_ADDCSV_NAME,
)

FILES_CHECKED = (CF_ADDAPI_NAME, CF_ADDCSV_NAME, REPORT_NAME, MANIFEST_NAME)

FILE_COUNT = 6


def _failed(directory: Path, code: str) -> ExportOutcome:
    return ExportOutcome(
        status="failed",
        directory=str(directory),
        files=[],
        errors=[code],
        validation=ValidationReport(ok=False, errors=[code], files_checked=[]),
        publishable=False,
        counts={},
    )


def stage_view(outcome: ExportOutcome) -> dict[str, Any]:
    """Render the export stage summary for the run report.

    The export directory is machine-private, so its summary carries
    ``visibility: private``; only the publish stage is marked public.
    """
    data = public_dump(outcome)
    data.pop("files", None)
    data["visibility"] = PRIVATE_VISIBILITY
    return data


def user_supplied_candidates(report: Mapping[str, Any]) -> bool:
    """Return True when the run report records user imported CF candidates."""
    entries = report.get("licenses")
    if not isinstance(entries, (list, tuple)):
        return False
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        tag = str(entry.get("license_tag") or "").strip().lower()
        if tag in USER_SUPPLIED_LICENSE_TAGS:
            return True
    return False


def _hashed(name: str, payload: bytes, entry_count: int) -> ExportedFile:
    return ExportedFile(
        name=name,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        entry_count=entry_count,
    )


def _outcome(
    *,
    directory: Path,
    validation: ValidationReport,
    counts: dict[str, int],
    publishable: bool,
    files: list[ExportedFile] | None = None,
) -> ExportOutcome:
    return ExportOutcome(
        status="ok",
        directory=str(directory),
        files=list(files or []),
        errors=list(validation.errors),
        validation=validation,
        publishable=publishable,
        counts=dict(counts),
    )


def build_export(
    directory: str | Path,
    report: Mapping[str, Any],
    *,
    nodes: Sequence[ProxyNode],
    edges: Sequence[EdgeEndpoint],
    proxies: Sequence[RankedProxy],
    endpoints: Sequence[RankedEndpoint],
    scoring_version: str,
    region_by_item: Mapping[str, str] | None = None,
    cf_candidates_authorized: bool = False,
    dls_min_speed_mb_s: float | None = None,
    addapi_remark_template: str | None = None,
) -> ExportOutcome:
    """Write the private export directory for one run and describe it.

    The directory under ``output/<run_id>/export`` is machine-private: it
    always holds every exported file, including proxy credentials, and is
    never filtered by the publish gates.
    """
    out_dir = Path(directory)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _failed(out_dir, "mkdir_failed")

    regions = region_by_item or {}
    cf_user_supplied = user_supplied_candidates(report)
    node_by_id = {node.item_id: node for node in nodes}
    edge_by_id = {edge.item_id: edge for edge in edges}
    ranked_proxies = sorted(
        (item for item in proxies if item.status == "ranked"),
        key=lambda item: item.rank,
    )
    ranked_endpoints = sorted(
        (item for item in endpoints if item.status == "ranked"),
        key=lambda item: item.rank,
    )

    uris: list[str] = []
    clash_entries: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    for ranked in ranked_proxies:
        node = node_by_id.get(ranked.item_id)
        if node is None:
            continue
        uri, uri_reason = build_proxy_uri(node)
        entry, clash_reason = build_clash_proxy(node)
        reason = uri_reason or clash_reason
        if uri is None or entry is None:
            key = reason or "skipped"
            skipped[key] = skipped.get(key, 0) + 1
            continue
        uris.append(uri)
        clash_entries.append(entry)

    edge_rows: list[tuple[EdgeEndpoint, RankedEndpoint]] = []
    for ranked in ranked_endpoints:
        edge = edge_by_id.get(ranked.item_id)
        if edge is None:
            continue
        edge_rows.append((edge, ranked))

    api_rows: list[tuple[str, int, str]] = []
    csv_rows: list[list[Any]] = []
    measured_endpoints = 0
    remark_template = (
        addapi_remark_template
        if addapi_remark_template is not None
        else DEFAULT_ADDAPI_REMARK_TEMPLATE
    )
    for edge, ranked in edge_rows:
        fallback_remark = str(edge.remarks or "") or str(regions.get(edge.item_id, "") or "")
        remark = addapi_remark_from_ranked(
            ranked,
            template=remark_template,
            fallback=fallback_remark,
        )
        api_rows.append((edge.address, int(edge.port), remark))
        params = edge.params or {}
        origin_port = params.get("origin_port", "")
        if ranked.latency_ms is not None or ranked.speed_mb_s is not None:
            measured_endpoints += 1
        csv_rows.append(
            [
                edge.address,
                int(edge.port),
                "" if origin_port is None else origin_port,
                edge.tls,
                params.get("datacenter", ""),
                params.get("region", ""),
                params.get("city", ""),
                ranked.latency_ms,
                ranked.speed_mb_s,
            ]
        )

    try:
        payloads = {
            PROXY_RAW_NAME: build_raw_text(uris).encode("utf-8"),
            PROXY_CLASH_NAME: build_clash_config(clash_entries).encode("utf-8"),
            CF_ADDAPI_NAME: build_addapi(api_rows).encode("utf-8"),
            CF_ADDCSV_NAME: build_addcsv(csv_rows).encode("utf-8"),
        }
        for name in CONTENT_NAMES:
            (out_dir / name).write_bytes(payloads[name])
    except OSError:
        return _failed(out_dir, "io_error")

    counts: dict[str, int] = {
        "proxies": len(uris),
        "endpoints": len(edge_rows),
        "files": FILE_COUNT,
        "skipped": sum(skipped.values()),
    }
    for reason in sorted(skipped):
        counts["skipped_{0}".format(reason)] = int(skipped[reason])

    first_errors = sorted(
        set(
            scan_text(CF_ADDAPI_NAME, payloads[CF_ADDAPI_NAME].decode("utf-8"))
            + scan_text(CF_ADDCSV_NAME, payloads[CF_ADDCSV_NAME].decode("utf-8"))
        )
    )
    files_checked = list(FILES_CHECKED)
    validation = ValidationReport(
        ok=not first_errors, errors=list(first_errors), files_checked=files_checked
    )
    content_written = bool(uris) or bool(edge_rows)
    publishable = bool(validation.ok and content_written)

    stage_report = dict(report)
    stages = dict(stage_report.get("stages") or {})
    stage_report["stages"] = stages
    pending = stage_report.get("stages_pending")
    if isinstance(pending, list):
        stage_report["stages_pending"] = [item for item in pending if item != "export"]
    stage_report["consumer_hints"] = build_consumer_hints(
        dls_min_speed_mb_s=dls_min_speed_mb_s,
        endpoint_count=len(edge_rows),
        measured_endpoint_count=measured_endpoints,
        addapi_remark_template=remark_template,
    )

    stages["export"] = stage_view(
        _outcome(
            directory=out_dir,
            validation=validation,
            counts=counts,
            publishable=publishable,
        )
    )
    try:
        write_json_atomic(out_dir / REPORT_NAME, stage_report)
    except OSError:
        return _failed(out_dir, "io_error")
    report_payload = report_bytes(stage_report)

    second_errors = sorted(
        set(
            first_errors
            + scan_text(REPORT_NAME, report_payload.decode("utf-8"))
        )
    )
    if second_errors != first_errors:
        validation = ValidationReport(
            ok=not second_errors, errors=list(second_errors), files_checked=files_checked
        )
        publishable = bool(validation.ok and content_written)
        stages["export"] = stage_view(
            _outcome(
                directory=out_dir,
                validation=validation,
                counts=counts,
                publishable=publishable,
            )
        )
        try:
            write_json_atomic(out_dir / REPORT_NAME, stage_report)
        except OSError:
            return _failed(out_dir, "io_error")
        report_payload = report_bytes(stage_report)

    entries = [
        _hashed(PROXY_RAW_NAME, payloads[PROXY_RAW_NAME], len(uris)),
        _hashed(PROXY_CLASH_NAME, payloads[PROXY_CLASH_NAME], len(clash_entries)),
        _hashed(CF_ADDAPI_NAME, payloads[CF_ADDAPI_NAME], len(api_rows)),
        _hashed(CF_ADDCSV_NAME, payloads[CF_ADDCSV_NAME], len(csv_rows)),
        _hashed(REPORT_NAME, report_payload, 0),
    ]

    manifest = _build_manifest(
        report,
        scoring_version,
        validation,
        publishable,
        entries,
        counts,
        cf_user_supplied,
        cf_candidates_authorized,
    )
    manifest_payload = dumps_json(manifest).encode("utf-8")
    manifest_errors = scan_text(MANIFEST_NAME, manifest_payload.decode("utf-8"))
    if manifest_errors:
        merged = sorted(set(list(validation.errors) + manifest_errors))
        validation = ValidationReport(
            ok=False, errors=merged, files_checked=files_checked
        )
        publishable = False
        stages["export"] = stage_view(
            _outcome(
                directory=out_dir,
                validation=validation,
                counts=counts,
                publishable=publishable,
            )
        )
        try:
            write_json_atomic(out_dir / REPORT_NAME, stage_report)
        except OSError:
            return _failed(out_dir, "io_error")
        report_payload = report_bytes(stage_report)
        entries[4] = _hashed(REPORT_NAME, report_payload, 0)
        manifest = _build_manifest(
            report,
            scoring_version,
            validation,
            publishable,
            entries,
            counts,
            cf_user_supplied,
            cf_candidates_authorized,
        )
        manifest_payload = dumps_json(manifest).encode("utf-8")

    try:
        write_json_atomic(out_dir / MANIFEST_NAME, manifest)
    except OSError:
        return _failed(out_dir, "io_error")

    files = list(entries) + [_hashed(MANIFEST_NAME, manifest_payload, 0)]
    files.sort(key=lambda item: item.name)
    return _outcome(
        directory=out_dir,
        validation=validation,
        counts=counts,
        publishable=publishable,
        files=files,
    )


def _build_manifest(
    report: Mapping[str, Any],
    scoring_version: str,
    validation: ValidationReport,
    publishable: bool,
    entries: list[ExportedFile],
    counts: dict[str, int],
    cf_candidates_user_supplied: bool,
    cf_candidates_authorized: bool,
) -> dict[str, Any]:
    return build_manifest(
        schema_version=int(report.get("schema_version", 1)),
        run_id=str(report.get("run_id", "")),
        runner_id=str(report.get("runner_id", "")),
        profile=str(report.get("profile", "")),
        generated_at=report.get("generated_at"),
        status=str(report.get("status", "")),
        scoring_version=scoring_version,
        validation=validation.model_dump(),
        publishable=publishable,
        files=entries,
        counts=counts,
        cf_candidates_user_supplied=cf_candidates_user_supplied,
        cf_candidates_authorized=cf_candidates_authorized,
    )
