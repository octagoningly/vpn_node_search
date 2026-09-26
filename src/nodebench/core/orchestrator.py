from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nodebench.core.config import AppConfig
from nodebench.core.context import get_logger, redact
from nodebench.core.errors import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_PROBE_OR_STORAGE,
    EXIT_SOURCES,
)
from nodebench.core.schema import SCHEMA_VERSION, ParseIssue, ProbeMode, ProbeStatus
from nodebench.core.serialization import public_dump
from nodebench.normalize import dedupe, normalize_all
from nodebench.parsers import parse_raw_item
from nodebench.probes import (
    CAPABILITY_SKIP_REASONS,
    CFST_BINARY_NAMES,
    MIHOMO_BINARY_NAMES,
    REASON_DISABLED,
    REASON_DRY_RUN,
    REASON_NO_CANDIDATES,
    REASON_NOT_RUN,
    MihomoProber,
    cf_budget,
    endpoint_target,
    make_cfst_prober,
    project_root,
    proxy_budget,
    proxy_target,
    resolve_binary,
    run_cf_batch,
    run_proxy_batch,
)
from nodebench.probes.cfst import BACKEND_NAME as CFST_BACKEND
from nodebench.probes.mihomo import BACKEND_NAME as MIHOMO_BACKEND
from nodebench.sources.collect import collect_all

PENDING_STAGES = ("inspect", "persist", "score", "export", "publish")
SOURCE_NAMES = ("local", "subscriptions", "github", "cf")
TRUNCATION_CODES = frozenset({"ip_limit_exceeded", "file_limit_exceeded"})
STRICT_EXEMPT_REASONS = frozenset({REASON_DISABLED, REASON_DRY_RUN, REASON_NOT_RUN})
PREVIEW_LIMIT = 20
PROXY_PREVIEW_FIELDS = (
    "item_id",
    "kind",
    "protocol",
    "port",
    "transport",
    "security",
    "source_ids",
    "remarks",
)
ENDPOINT_PREVIEW_FIELDS = (
    "item_id",
    "kind",
    "address",
    "port",
    "target_host",
    "tls",
    "source_ids",
    "remarks",
)


def _generated_at() -> str:
    moment = datetime.now(timezone.utc).replace(microsecond=0)
    return moment.isoformat().replace("+00:00", "Z")


def _parse_items(items: list[Any]) -> tuple[list, list, list]:
    proxies: list = []
    endpoints: list = []
    issues: list[ParseIssue] = []
    for item in items:
        try:
            item_proxies, item_endpoints, item_issues = parse_raw_item(item)
        except Exception as err:
            issues.append(
                ParseIssue(
                    source_id=item.source_id,
                    code="parse_error",
                    message_redacted=redact(f"{type(err).__name__}: {err}"),
                    raw_ref=item.source_ref or "",
                )
            )
            continue
        proxies.extend(item_proxies)
        endpoints.extend(item_endpoints)
        issues.extend(item_issues)
    return proxies, endpoints, issues


def _status(reports: list, node_count: int, edge_count: int, issue_count: int) -> str:
    if node_count == 0 and edge_count == 0:
        return "failed"
    if reports and all(not report.ok for report in reports):
        return "failed"
    if issue_count or any(not report.ok for report in reports):
        return "partial"
    return "ok"


def _diagnostics(reports: list, issues: list) -> list[str]:
    lines: list[str] = []
    for report in reports:
        for error in report.errors:
            lines.append(
                "{0}:{1}: {2}".format(
                    report.source_id, error.code, redact(error.message_redacted)
                )
            )
    for issue in issues:
        lines.append(
            "{0}:{1}: {2}".format(
                issue.source_id, issue.code, redact(issue.message_redacted)
            )
        )
    return lines


def _limits(config: AppConfig, reports: list) -> dict:
    sources = config.sources
    enabled = {
        "local": sources.local.enabled,
        "subscriptions": sources.subscriptions.enabled,
        "github": sources.github.enabled,
        "cf": sources.cf.enabled,
    }
    return {
        "budget": {str(name): float(value) for name, value in config.budget.items()},
        "caps": {
            "cf_max_ips_per_run": int(sources.cf.max_ips_per_run),
            "github_max_files_per_run": int(sources.github.max_files_per_run),
        },
        "truncated_sources": [
            report.source_id
            for report in reports
            if any(error.code in TRUNCATION_CODES for error in report.errors)
        ],
        "failed_sources": [
            report.source_id for report in reports if not report.ok
        ],
        "disabled_sources": [
            name for name in SOURCE_NAMES if not enabled.get(name, False)
        ],
    }


def _preview_item(item: Any, fields: tuple[str, ...]) -> dict:
    preview: dict[str, Any] = {}
    for field in fields:
        value = getattr(item, field)
        if field == "target_host" and not value:
            value = None
        preview[field] = value
    return preview


def _output_base(config: AppConfig) -> Path:
    base = Path(str(config.output_dir or "output"))
    if not base.is_absolute():
        base = project_root() / base
    return base


def _empty_probe_node(backend: str, reason: str) -> dict[str, Any]:
    return {
        "mode": "skip",
        "backend": backend,
        "backend_version": "",
        "skipped_reason": reason,
        "attempted": 0,
        "ok": 0,
        "failed": 0,
        "timeout": 0,
        "measurement_error": 0,
        "incompatible": 0,
        "usable_real": 0,
        "skipped": 0,
        "simulated_results": 0,
    }


def _probe_summary(results: list, backend: str) -> dict[str, Any]:
    if not results:
        return _empty_probe_node(backend, REASON_NO_CANDIDATES)
    counts = {status.value: 0 for status in ProbeStatus}
    attempted = 0
    usable = 0
    simulated = 0
    real_seen = False
    version = ""
    for result in results:
        state = str(getattr(result.status, "value", result.status))
        counts[state] = counts.get(state, 0) + 1
        if state != ProbeStatus.SKIPPED.value:
            attempted += 1
        mode = str(getattr(result.probe_mode, "value", result.probe_mode))
        if mode == ProbeMode.SIMULATED.value:
            simulated += 1
        elif state != ProbeStatus.SKIPPED.value:
            real_seen = True
        if state == ProbeStatus.OK.value and mode == ProbeMode.REAL.value:
            usable += 1
        if not version and result.backend_version:
            version = str(result.backend_version)
    if real_seen:
        mode_out = ProbeMode.REAL.value
        reason = ""
    elif attempted:
        mode_out = ProbeMode.SIMULATED.value
        reason = ""
    else:
        mode_out = "skip"
        reason = next(
            (
                str(result.skipped_reason)
                for result in results
                if str(result.skipped_reason).strip()
            ),
            REASON_NO_CANDIDATES,
        )
    return {
        "mode": mode_out,
        "backend": str(results[0].backend or backend),
        "backend_version": version,
        "skipped_reason": reason,
        "attempted": attempted,
        "ok": counts.get(ProbeStatus.OK.value, 0),
        "failed": counts.get(ProbeStatus.FAIL.value, 0),
        "timeout": counts.get(ProbeStatus.TIMEOUT.value, 0),
        "measurement_error": counts.get(ProbeStatus.MEASUREMENT_ERROR.value, 0),
        "incompatible": counts.get(ProbeStatus.INCOMPATIBLE.value, 0),
        "usable_real": usable,
        "skipped": counts.get(ProbeStatus.SKIPPED.value, 0),
        "simulated_results": simulated,
    }


def _probe_status_map(results: list) -> dict[str, str]:
    return {
        str(result.item_id): str(getattr(result.status, "value", result.status))
        for result in results
    }


def _preview_with_status(
    item: Any, fields: tuple[str, ...], statuses: Mapping[str, str]
) -> dict:
    preview = _preview_item(item, fields)
    preview["probe_status"] = statuses.get(str(item.item_id), "not_probed")
    return preview


def _run_proxy_probe(
    config: AppConfig,
    ctx: Any,
    *,
    run_probes: bool,
    dry_run: bool,
    proxies: list,
    logger: Any,
) -> tuple[dict, list]:
    backend = MIHOMO_BACKEND
    if not run_probes:
        return _empty_probe_node(backend, REASON_NOT_RUN), []
    if dry_run:
        return _empty_probe_node(backend, REASON_DRY_RUN), []
    probe_config = config.probe.proxy
    if not probe_config.enabled:
        return _empty_probe_node(backend, REASON_DISABLED), []
    if not proxies:
        return _empty_probe_node(backend, REASON_NO_CANDIDATES), []
    budget = proxy_budget(config)
    binary = resolve_binary(probe_config.mihomo_path, MIHOMO_BINARY_NAMES)
    prober = MihomoProber(
        config,
        budget,
        run_id=ctx.run_id,
        runner_id=ctx.runner_id,
        run_dir=_output_base(config) / ctx.run_id,
        binary=binary,
    )
    targets = [proxy_target(node) for node in proxies]
    try:
        results = run_proxy_batch(targets, prober, budget)
    except Exception as err:
        logger.error(
            "probe proxy failed: {0}".format(
                redact(f"{type(err).__name__}: {err}")
            )
        )
        results = [prober.failure(target, err) for target in targets]
    return _probe_summary(results, backend), results


def _run_cf_probe(
    config: AppConfig,
    ctx: Any,
    *,
    run_probes: bool,
    dry_run: bool,
    edges: list,
    logger: Any,
) -> tuple[dict, list]:
    backend = CFST_BACKEND
    if not run_probes:
        return _empty_probe_node(backend, REASON_NOT_RUN), []
    if dry_run:
        return _empty_probe_node(backend, REASON_DRY_RUN), []
    probe_config = config.probe.cf
    if not probe_config.enabled:
        return _empty_probe_node(backend, REASON_DISABLED), []
    if not edges:
        return _empty_probe_node(backend, REASON_NO_CANDIDATES), []
    budget = cf_budget(config)
    prober = make_cfst_prober(
        config,
        budget,
        run_id=ctx.run_id,
        runner_id=ctx.runner_id,
        run_dir=_output_base(config) / ctx.run_id,
    )
    targets = [endpoint_target(edge) for edge in edges]
    try:
        results = run_cf_batch(targets, prober, budget)
    except Exception as err:
        logger.error(
            "probe cf failed: {0}".format(redact(f"{type(err).__name__}: {err}"))
        )
        results = [prober.failure(target, err) for target in targets]
    return _probe_summary(results, backend), results


def _probe_degraded(probe: Mapping[str, Any]) -> bool:
    for node in probe.values():
        if (
            node.get("mode") == "skip"
            and str(node.get("skipped_reason") or "") in CAPABILITY_SKIP_REASONS
        ):
            return True
        if (
            node.get("mode") == ProbeMode.REAL.value
            and int(node.get("attempted", 0)) > 0
            and int(node.get("ok", 0)) == 0
            and int(node.get("usable_real", 0)) == 0
            and int(node.get("failed", 0)) + int(node.get("timeout", 0)) > 0
        ):
            return True
    return False


def _status_with_probe(
    status: str, probe: Mapping[str, Any], *, dry_run: bool, run_probes: bool
) -> str:
    if dry_run or not run_probes or status == "failed":
        return status
    if status == "ok" and _probe_degraded(probe):
        return "partial"
    return status


def resolve_run_exit(
    *,
    run_status: str = "ok",
    probe: Mapping[str, Any] | None = None,
    probe_results: Sequence[Any] | None = None,
    cf_enabled: bool = False,
    target_host: str = "",
    strict: bool = False,
) -> int:
    if cf_enabled and not str(target_host or "").strip():
        return EXIT_CONFIG
    for result in probe_results or ():
        if isinstance(result, Mapping):
            code = str(result.get("error_code") or "")
        else:
            code = str(getattr(result, "error_code", "") or "")
        if code == "config_error":
            return EXIT_CONFIG
    for node in (probe or {}).values():
        if (
            node.get("mode") == ProbeMode.REAL.value
            and int(node.get("attempted", 0)) > 0
            and int(node.get("ok", 0)) == 0
            and int(node.get("usable_real", 0)) == 0
            and int(node.get("failed", 0)) + int(node.get("timeout", 0)) > 0
        ):
            return EXIT_PROBE_OR_STORAGE
    if run_status == "failed":
        return EXIT_SOURCES
    if strict:
        for node in (probe or {}).values():
            reason = str(node.get("skipped_reason") or "")
            if node.get("mode") == "skip" and reason not in STRICT_EXEMPT_REASONS:
                return EXIT_PROBE_OR_STORAGE
    return EXIT_OK


def run_pipeline(
    config: AppConfig,
    ctx: Any,
    *,
    dry_run: bool = False,
    run_probes: bool = True,
    probe_sink: list | None = None,
) -> dict:
    """Collect, parse, normalize, dedupe and probe sources into a run summary."""
    logger = get_logger(ctx.run_id)
    outcome = collect_all(config, ctx)
    reports = list(outcome.reports)
    proxies, endpoints, parse_issues = _parse_items(outcome.items)
    nodes, edges, collected_issues = normalize_all(
        proxies, endpoints, parse_issues
    )
    final_proxies, final_edges = dedupe(nodes, edges)
    counts = {
        "sources_total": len(reports),
        "sources_failed": sum(1 for report in reports if not report.ok),
        "raw_items": len(outcome.items),
        "parsed_proxies": len(proxies),
        "parsed_endpoints": len(endpoints),
        "parse_issues": len(collected_issues),
        "proxy_nodes": len(final_proxies),
        "edge_endpoints": len(final_edges),
        "dupes_merged": (len(nodes) + len(edges))
        - (len(final_proxies) + len(final_edges)),
    }
    status = _status(
        reports,
        counts["proxy_nodes"],
        counts["edge_endpoints"],
        counts["parse_issues"],
    )
    proxy_node, proxy_results = _run_proxy_probe(
        config,
        ctx,
        run_probes=run_probes,
        dry_run=dry_run,
        proxies=final_proxies,
        logger=logger,
    )
    cf_node, cf_results = _run_cf_probe(
        config,
        ctx,
        run_probes=run_probes,
        dry_run=dry_run,
        edges=final_edges,
        logger=logger,
    )
    probe = {"proxy": proxy_node, "cf": cf_node}
    probe_results = [*proxy_results, *cf_results]
    if probe_sink is not None:
        probe_sink.extend(probe_results)
    status = _status_with_probe(
        status, probe, dry_run=dry_run, run_probes=run_probes
    )
    generated_at = _generated_at()
    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": ctx.run_id,
        "runner_id": ctx.runner_id,
        "profile": config.profile,
        "generated_at": generated_at,
        "dry_run": bool(dry_run),
        "stages_pending": list(PENDING_STAGES),
        "status": status,
        "diagnostics": _diagnostics(reports, collected_issues),
        "counts": counts,
        "limits": _limits(config, reports),
        "source_reports": reports,
        "issues": collected_issues,
        "probe": probe,
        "items_preview": {
            "proxy_nodes": [
                _preview_with_status(
                    node, PROXY_PREVIEW_FIELDS, _probe_status_map(proxy_results)
                )
                for node in final_proxies[:PREVIEW_LIMIT]
            ],
            "edge_endpoints": [
                _preview_with_status(
                    edge, ENDPOINT_PREVIEW_FIELDS, _probe_status_map(cf_results)
                )
                for edge in final_edges[:PREVIEW_LIMIT]
            ],
        },
    }
    logger.info(
        "status={0} raw_items={1} proxy_nodes={2} edge_endpoints={3} "
        "issues={4} sources={5}/{6}".format(
            status,
            counts["raw_items"],
            counts["proxy_nodes"],
            counts["edge_endpoints"],
            counts["parse_issues"],
            counts["sources_total"] - counts["sources_failed"],
            counts["sources_total"],
        )
    )
    logger.info(
        "probe proxy mode={0} attempted={1} usable_real={2} skipped={3} "
        "cf mode={4} attempted={5} usable_real={6} skipped={7}".format(
            proxy_node["mode"],
            proxy_node["attempted"],
            proxy_node["usable_real"],
            proxy_node["skipped"],
            cf_node["mode"],
            cf_node["attempted"],
            cf_node["usable_real"],
            cf_node["skipped"],
        )
    )
    return public_dump(result)


__all__ = [
    "PENDING_STAGES",
    "resolve_run_exit",
    "run_pipeline",
]
