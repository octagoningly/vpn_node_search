from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from nodebench.core.config import AppConfig
from nodebench.core.context import get_logger, redact
from nodebench.core.schema import SCHEMA_VERSION, ParseIssue
from nodebench.core.serialization import public_dump
from nodebench.normalize import dedupe, normalize_all
from nodebench.parsers import parse_raw_item
from nodebench.sources.collect import collect_all

PENDING_STAGES = ("probe", "inspect", "persist", "score", "export", "publish")
SOURCE_NAMES = ("local", "subscriptions", "github", "cf")
TRUNCATION_CODES = frozenset({"ip_limit_exceeded", "file_limit_exceeded"})
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


def run_pipeline(config: AppConfig, ctx: Any, *, dry_run: bool = False) -> dict:
    """Collect, parse, normalize and dedupe sources into a public run summary."""
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
    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": ctx.run_id,
        "runner_id": ctx.runner_id,
        "profile": config.profile,
        "generated_at": _generated_at(),
        "dry_run": bool(dry_run),
        "stages_pending": list(PENDING_STAGES),
        "status": status,
        "diagnostics": _diagnostics(reports, collected_issues),
        "counts": counts,
        "limits": _limits(config, reports),
        "source_reports": reports,
        "issues": collected_issues,
        "items_preview": {
            "proxy_nodes": [
                _preview_item(node, PROXY_PREVIEW_FIELDS)
                for node in final_proxies[:PREVIEW_LIMIT]
            ],
            "edge_endpoints": [
                _preview_item(edge, ENDPOINT_PREVIEW_FIELDS)
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
    return public_dump(result)


__all__ = ["PENDING_STAGES", "run_pipeline"]
