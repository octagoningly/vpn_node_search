from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nodebench.core.config import AppConfig
from nodebench.core.context import register_secrets, redact
from nodebench.core.errors import ConfigError, NodeBenchError, StorageError
from nodebench.core.schema import (
    EdgeEndpoint,
    ExportOutcome,
    ExportedFile,
    ProxyNode,
    ProxyProbeResult,
    PublishResult,
    RawItem,
    ScoreReport,
    SourceReport,
    ValidationReport,
)
from nodebench.core.serialization import public_dump, write_json_atomic
from nodebench.exporters import build_export
from nodebench.exporters.build import stage_view
from nodebench.exporters.manifest import MANIFEST_NAME
from nodebench.history import db as dbmod
from nodebench.history.stats import history_for_items
from nodebench.history.store import (
    ENDPOINT_TEST_TYPE,
    PROXY_TEST_TYPE,
    decode_probe_results,
    load_entities,
    persist_run,
    record_intelligence,
)
from nodebench.intelligence import IntelligenceService
from nodebench.probes import project_root
from nodebench.publishing import is_redistributable, publish_output
from nodebench.publishing.upload import upload_published_files
from nodebench.scoring import SCORING_VERSION, make_geo_lookup, score_run

STAGE_ORDER = ("inspect", "persist", "score", "export", "publish")
EXPORT_DIR_NAME = "export"
LATEST_DIR_NAME = "latest"
SCORED_NAME = "scored.json"
PROBE_RESULTS_NAME = "probe-results.json"
RUN_REPORT_NAME = "run-report.json"
INTELLIGENCE_NAME = "intelligence-report.json"


def output_base(config: AppConfig) -> Path:
    base = Path(str(config.output_dir or "output"))
    if not base.is_absolute():
        base = project_root() / base
    return base


def database_path(config: AppConfig) -> Path:
    return output_base(config) / dbmod.DB_FILENAME


def scored_path(config: AppConfig, run_id: str) -> Path:
    return output_base(config) / run_id / SCORED_NAME


def intelligence_path(config: AppConfig, run_id: str) -> Path:
    return output_base(config) / run_id / INTELLIGENCE_NAME


def export_dir(config: AppConfig, run_id: str) -> Path:
    return output_base(config) / run_id / EXPORT_DIR_NAME


def error_text(err: BaseException) -> str:
    if isinstance(err, NodeBenchError):
        return redact("{0}: {1}".format(err.code, err.message))
    return redact("{0}: {1}".format(type(err).__name__, err))


def license_entries(items: Iterable[RawItem]) -> list[dict[str, Any]]:
    tags: dict[str, str] = {}
    for item in items:
        source_id = str(item.source_id)
        if source_id in tags:
            continue
        tags[source_id] = str(item.license_tag or "unknown") or "unknown"
    return [
        {
            "source_id": source_id,
            "license_tag": tags[source_id],
            "redistributable": is_redistributable(tags[source_id]),
        }
        for source_id in sorted(tags)
    ]


def report_time(report: Mapping[str, Any]) -> datetime:
    stamp = str(report.get("generated_at") or "")
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return datetime.now(timezone.utc).replace(microsecond=0)


def _history_maps(
    config: AppConfig, run_id: str, runner_id: str, now: datetime
) -> tuple[dict[str, Any], dict[str, Any]]:
    db_path = database_path(config)
    if not db_path.is_file():
        raise StorageError(
            code="database_missing",
            message=f"history database not found: {db_path}",
        )
    conn = dbmod.open_db(db_path)
    try:
        kwargs = dict(
            runner_id=runner_id,
            exclude_run_id=run_id,
            windows=[int(config.history.days)],
            min_samples=int(config.scoring.filters.stability_min_samples),
            now=now,
        )
        proxy_history = history_for_items(
            conn, test_type=PROXY_TEST_TYPE, **kwargs
        )
        endpoint_history = history_for_items(
            conn, test_type=ENDPOINT_TEST_TYPE, **kwargs
        )
    finally:
        conn.close()
    return proxy_history, endpoint_history


def run_persist_stage(
    config: AppConfig,
    report: Mapping[str, Any],
    *,
    source_reports: Sequence[SourceReport],
    nodes: Sequence[ProxyNode],
    edges: Sequence[EdgeEndpoint],
    probe_results: Sequence[Any],
    items: Sequence[RawItem],
) -> dict[str, Any]:
    summary = persist_run(
        database_path(config),
        run_id=str(report["run_id"]),
        runner_id=str(report["runner_id"]),
        profile=str(report["profile"]),
        status=str(report["status"]),
        counts={str(key): int(value) for key, value in report["counts"].items()},
        source_reports=source_reports,
        license_tags={
            str(item.source_id): str(item.license_tag or "unknown") for item in items
        },
        nodes=nodes,
        edges=edges,
        probe_results=probe_results,
        window_days=int(config.history.days),
        retention_days=config.history.retention_days,
    )
    return public_dump(summary)


def run_inspect_stage(
    config: AppConfig,
    report: Mapping[str, Any],
    *,
    probe_results: Sequence[Any],
) -> dict[str, Any]:
    """Collect exit IP / Geo / reputation intelligence for probed nodes."""
    run_id = str(report["run_id"])
    runner_id = str(report["runner_id"])
    service = IntelligenceService(config.intelligence)
    results = [item for item in probe_results if isinstance(item, ProxyProbeResult)]
    intel = service.inspect_results(results, run_id=run_id, runner_id=runner_id)
    write_json_atomic(intelligence_path(config, run_id), public_dump(intel))
    persisted = 0
    db_path = database_path(config)
    if db_path.is_file():
        persisted = record_intelligence(db_path, intel)
    return {
        "status": "ok",
        "counts": dict(intel.counts),
        "persisted": persisted,
        "report": str(intelligence_path(config, run_id)),
    }


def run_score_stage(
    config: AppConfig,
    report: Mapping[str, Any],
    *,
    nodes: Sequence[ProxyNode],
    edges: Sequence[EdgeEndpoint],
    probe_results: Sequence[Any],
) -> tuple[ScoreReport, dict[str, Any]]:
    run_id = str(report["run_id"])
    proxy_history, endpoint_history = _history_maps(
        config, run_id, str(report["runner_id"]), report_time(report)
    )
    geo_lookup = make_geo_lookup(
        base_url=str(config.intelligence.geo_url or ""),
        timeout=float(config.intelligence.timeout),
    )
    score_report = score_run(
        run_id=run_id,
        runner_id=str(report["runner_id"]),
        profile=str(report["profile"]),
        nodes=nodes,
        edges=edges,
        probe_results=probe_results,
        proxy_history=proxy_history,
        endpoint_history=endpoint_history,
        scoring=config.scoring,
        history_days=int(config.history.days),
        geo_lookup=geo_lookup,
    )
    write_json_atomic(scored_path(config, run_id), public_dump(score_report))
    summary = {
        "status": "ok",
        "counts": dict(score_report.counts),
        "issues": [public_dump(issue) for issue in score_report.issues],
    }
    return score_report, summary


def run_export_stage(
    config: AppConfig,
    report: Mapping[str, Any],
    *,
    nodes: Sequence[ProxyNode],
    edges: Sequence[EdgeEndpoint],
    score_report: ScoreReport,
) -> ExportOutcome:
    return build_export(
        export_dir(config, str(report["run_id"])),
        report,
        nodes=nodes,
        edges=edges,
        proxies=score_report.proxies,
        endpoints=score_report.endpoints,
        scoring_version=SCORING_VERSION,
        cf_candidates_authorized=bool(config.publish.cf_candidates_authorized),
        dls_min_speed_mb_s=float(config.scoring.filters.min_speed_mb_s),
        addapi_remark_template=config.addapi_remark_template,
    )


def _tags_for(
    ranked_items: Sequence[Any], tag_by_source: Mapping[str, str]
) -> set[str]:
    tags: set[str] = set()
    for item in ranked_items:
        if getattr(item, "status", "") != "ranked":
            continue
        for source_id in getattr(item, "source_ids", []) or []:
            tags.add(tag_by_source.get(str(source_id), "unknown"))
    return tags


def _ranked_count(ranked_items: Sequence[Any]) -> int:
    return sum(1 for item in ranked_items if getattr(item, "status", "") == "ranked")


def run_publish_stage(
    config: AppConfig,
    report: Mapping[str, Any],
    *,
    outcome: ExportOutcome,
    score_report: ScoreReport,
    licenses: Sequence[Mapping[str, Any]],
    allow_publish: bool,
    enabled: bool | None = None,
    run_upload: bool = True,
) -> dict[str, Any]:
    tag_by_source = {
        str(entry.get("source_id") or ""): str(entry.get("license_tag") or "unknown")
        for entry in licenses
    }
    run_id = str(report["run_id"])
    publish_enabled = bool(config.publish.enabled if enabled is None else enabled)
    result = publish_output(
        export_dir=export_dir(config, run_id),
        target_dir=output_base(config) / LATEST_DIR_NAME,
        report=report,
        outcome=outcome,
        ranked_proxies=_ranked_count(score_report.proxies),
        ranked_endpoints=_ranked_count(score_report.endpoints),
        proxy_license_tags=_tags_for(score_report.proxies, tag_by_source),
        endpoint_license_tags=_tags_for(score_report.endpoints, tag_by_source),
        enabled=publish_enabled,
        allow_publish=bool(allow_publish),
        allow_proxy_credentials=bool(config.publish.allow_proxy_credentials),
        cf_candidates_authorized=bool(config.publish.cf_candidates_authorized),
    )
    if run_upload and result.status == "ok" and bool(config.publish.upload.enabled):
        try:
            urls = upload_published_files(
                config.publish.upload,
                output_base(config) / LATEST_DIR_NAME,
                base_name=run_id,
            )
        except Exception as err:
            result = result.model_copy(
                update={
                    "status": "failed",
                    "reason": "upload_failed",
                    "errors": [error_text(err)],
                    "public_urls": [],
                }
            )
        else:
            result = result.model_copy(update={"public_urls": list(urls)})
    return public_dump(result)


def run_post_stages(
    report: dict[str, Any],
    *,
    config: AppConfig,
    source_reports: Sequence[SourceReport],
    nodes: Sequence[ProxyNode],
    edges: Sequence[EdgeEndpoint],
    probe_results: Sequence[Any],
    items: Sequence[RawItem],
    allow_publish: bool = True,
) -> dict[str, Any]:
    """Run persist, score, export and publish, recording one summary per stage."""
    stages: dict[str, Any] = dict(report.get("stages") or {})
    pending: list[str] = list(report.get("stages_pending") or [])
    licenses = license_entries(items)
    report["licenses"] = licenses

    score_report: ScoreReport | None = None
    outcome: ExportOutcome | None = None
    for name in STAGE_ORDER:
        report["stages"] = stages
        report["stages_pending"] = pending
        try:
            if name == "inspect":
                summary = run_inspect_stage(
                    config,
                    report,
                    probe_results=probe_results,
                )
            elif name == "persist":
                summary = run_persist_stage(
                    config,
                    report,
                    source_reports=source_reports,
                    nodes=nodes,
                    edges=edges,
                    probe_results=probe_results,
                    items=items,
                )
            elif name == "score":
                score_report, summary = run_score_stage(
                    config,
                    report,
                    nodes=nodes,
                    edges=edges,
                    probe_results=probe_results,
                )
            elif name == "export":
                if score_report is None:
                    raise ConfigError(
                        code="score_missing",
                        message="score stage did not produce a report",
                    )
                outcome = run_export_stage(
                    config,
                    report,
                    nodes=nodes,
                    edges=edges,
                    score_report=score_report,
                )
                summary = stage_view(outcome)
            else:
                if outcome is None or score_report is None:
                    raise ConfigError(
                        code="export_missing",
                        message="export stage did not produce an outcome",
                    )
                summary = run_publish_stage(
                    config,
                    report,
                    outcome=outcome,
                    score_report=score_report,
                    licenses=licenses,
                    allow_publish=allow_publish,
                )
        except Exception as err:
            stages[name] = {"status": "failed", "errors": [error_text(err)]}
            report["stages"] = stages
            if name in pending:
                pending.remove(name)
            report["stages_pending"] = pending
            break
        stages[name] = summary
        report["stages"] = stages
        if name in pending:
            pending.remove(name)
        report["stages_pending"] = pending
    report["stages"] = stages
    report["stages_pending"] = pending
    return report


def register_node_secrets(nodes: Iterable[ProxyNode]) -> None:
    values: list[str] = []
    for node in nodes:
        for value in (node.secrets or {}).values():
            if isinstance(value, str):
                values.append(value)
    register_secrets(values)


def _read_json(path: Path, missing_code: str) -> Any:
    if not path.is_file():
        raise ConfigError(
            code=missing_code,
            message=f"required artifact not found: {path}",
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ConfigError(
            code="artifact_unreadable",
            message=f"cannot read {path}: {type(err).__name__}: {err}",
        ) from err


def load_probe_results(config: AppConfig, run_id: str) -> list[Any]:
    payload = _read_json(
        output_base(config) / run_id / PROBE_RESULTS_NAME, "probe_results_missing"
    )
    if not isinstance(payload, Mapping):
        raise ConfigError(
            code="probe_results_invalid",
            message="probe results artifact must be an object",
        )
    try:
        return decode_probe_results(payload)
    except NodeBenchError as err:
        raise ConfigError(
            code="probe_results_invalid", message=error_text(err)
        ) from err


def load_run_report(config: AppConfig, run_id: str) -> dict[str, Any]:
    payload = _read_json(
        output_base(config) / run_id / RUN_REPORT_NAME, "run_report_missing"
    )
    if not isinstance(payload, dict):
        raise ConfigError(
            code="run_report_invalid",
            message="run report artifact must be an object",
        )
    return payload


def load_score_report(config: AppConfig, run_id: str) -> ScoreReport:
    payload = _read_json(scored_path(config, run_id), "score_report_missing")
    try:
        return ScoreReport.model_validate(payload)
    except (ValidationError, ValueError) as err:
        raise ConfigError(
            code="score_report_invalid",
            message=f"score report artifact is not valid: {err}",
        ) from err


def score_artifacts(config: AppConfig, run_id: str) -> ScoreReport:
    """Rebuild a score report from persisted artifacts (standalone score stage)."""
    results = load_probe_results(config, run_id)
    nodes, edges = load_entities(database_path(config), run_id)
    proxy_history, endpoint_history = _history_maps(
        config, run_id, str(config.runner_id), datetime.now(timezone.utc)
    )
    geo_lookup = make_geo_lookup(
        base_url=str(config.intelligence.geo_url or ""),
        timeout=float(config.intelligence.timeout),
    )
    score_report = score_run(
        run_id=run_id,
        runner_id=str(config.runner_id),
        profile=str(config.profile),
        nodes=nodes,
        edges=edges,
        probe_results=results,
        proxy_history=proxy_history,
        endpoint_history=endpoint_history,
        scoring=config.scoring,
        history_days=int(config.history.days),
        geo_lookup=geo_lookup,
    )
    write_json_atomic(scored_path(config, run_id), public_dump(score_report))
    return score_report


def inspect_artifacts(config: AppConfig, run_id: str) -> dict[str, Any]:
    """Rebuild intelligence from persisted probe results (standalone inspect)."""
    results = load_probe_results(config, run_id)
    report = load_run_report(config, run_id)
    summary = run_inspect_stage(config, report, probe_results=results)
    payload = _read_json(intelligence_path(config, run_id), "intelligence_report_missing")
    summary["report_payload"] = payload
    return summary


def export_artifacts(config: AppConfig, run_id: str) -> ExportOutcome:
    """Rebuild export artifacts from persisted inputs (standalone export stage)."""
    report = load_run_report(config, run_id)
    score_report = load_score_report(config, run_id)
    nodes, edges = load_entities(database_path(config), run_id)
    register_node_secrets(nodes)
    return build_export(
        export_dir(config, run_id),
        report,
        nodes=nodes,
        edges=edges,
        proxies=score_report.proxies,
        endpoints=score_report.endpoints,
        scoring_version=SCORING_VERSION,
        cf_candidates_authorized=bool(config.publish.cf_candidates_authorized),
        dls_min_speed_mb_s=float(config.scoring.filters.min_speed_mb_s),
        addapi_remark_template=config.addapi_remark_template,
    )


def load_export_outcome(config: AppConfig, run_id: str) -> ExportOutcome:
    """Describe an existing export directory without rewriting it."""
    directory = export_dir(config, run_id)
    payload = _read_json(directory / MANIFEST_NAME, "export_missing")
    if not isinstance(payload, dict):
        raise ConfigError(
            code="export_invalid",
            message="export manifest must be an object",
        )
    files: list[ExportedFile] = []
    entries = payload.get("files")
    if isinstance(entries, list):
        for item in entries:
            if not isinstance(item, Mapping):
                continue
            files.append(
                ExportedFile(
                    name=str(item.get("name") or ""),
                    sha256=str(item.get("sha256") or ""),
                    size_bytes=int(item.get("size_bytes") or 0),
                    entry_count=int(item.get("entry_count") or 0),
                )
            )
    manifest_path = directory / MANIFEST_NAME
    raw_manifest = manifest_path.read_bytes()
    files.append(
        ExportedFile(
            name=MANIFEST_NAME,
            sha256=hashlib.sha256(raw_manifest).hexdigest(),
            size_bytes=len(raw_manifest),
            entry_count=0,
        )
    )
    files.sort(key=lambda item: item.name)
    validation_raw = payload.get("validation")
    if isinstance(validation_raw, Mapping):
        validation = ValidationReport(
            ok=bool(validation_raw.get("ok")),
            errors=[str(entry) for entry in validation_raw.get("errors") or []],
            files_checked=[
                str(entry) for entry in validation_raw.get("files_checked") or []
            ],
        )
    else:
        validation = ValidationReport(ok=True, errors=[], files_checked=[])
    counts_raw = payload.get("counts")
    counts = (
        {str(key): int(value) for key, value in counts_raw.items()}
        if isinstance(counts_raw, Mapping)
        else {}
    )
    return ExportOutcome(
        status="ok",
        directory=str(directory),
        files=files,
        errors=list(validation.errors),
        validation=validation,
        publishable=bool(payload.get("publishable")),
        counts=counts,
    )


def publish_artifacts(
    config: AppConfig,
    run_id: str,
    *,
    allow_publish: bool = True,
    enabled: bool | None = True,
) -> dict[str, Any]:
    """Standalone publish: gate an existing export, then optionally upload.

    ``enabled`` defaults to True because ``nodebench publish`` is the
    explicit release step; pass ``None`` to honour ``publish.enabled``.
    """
    report = load_run_report(config, run_id)
    score_report = load_score_report(config, run_id)
    outcome = load_export_outcome(config, run_id)
    licenses_raw = report.get("licenses")
    licenses: Sequence[Mapping[str, Any]] = (
        [entry for entry in licenses_raw if isinstance(entry, Mapping)]
        if isinstance(licenses_raw, list)
        else []
    )
    return run_publish_stage(
        config,
        report,
        outcome=outcome,
        score_report=score_report,
        licenses=licenses,
        allow_publish=allow_publish,
        enabled=enabled,
    )


__all__ = [
    "STAGE_ORDER",
    "EXPORT_DIR_NAME",
    "LATEST_DIR_NAME",
    "PROBE_RESULTS_NAME",
    "RUN_REPORT_NAME",
    "SCORED_NAME",
    "INTELLIGENCE_NAME",
    "error_text",
    "export_artifacts",
    "export_dir",
    "database_path",
    "inspect_artifacts",
    "intelligence_path",
    "license_entries",
    "load_export_outcome",
    "load_probe_results",
    "load_run_report",
    "load_score_report",
    "output_base",
    "publish_artifacts",
    "register_node_secrets",
    "run_export_stage",
    "run_inspect_stage",
    "run_persist_stage",
    "run_post_stages",
    "run_publish_stage",
    "run_score_stage",
    "score_artifacts",
    "scored_path",
]
