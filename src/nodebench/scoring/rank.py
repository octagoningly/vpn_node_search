from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from nodebench.core.config import ScoringConfig
from nodebench.core.schema import (
    EdgeEndpoint,
    EndpointProbeResult,
    HistorySummary,
    ProbeMode,
    ProbeStatus,
    RankedEndpoint,
    RankedProxy,
    ProxyNode,
    ProxyProbeResult,
    ScoreIssue,
    ScoreReport,
)
from nodebench.scoring.rules import (
    SCORING_VERSION,
    SPEED_REF_MB_S,
    STABILITY_INSUFFICIENT_PENALTY,
    latency_score,
    loss_score,
    purity_score,
    speed_score,
    stability_score,
    weighted_score,
)

MEASURED_FAILURE_STATUSES = frozenset(
    {
        ProbeStatus.FAIL.value,
        ProbeStatus.TIMEOUT.value,
        ProbeStatus.MEASUREMENT_ERROR.value,
        ProbeStatus.INCOMPATIBLE.value,
    }
)
MISSING_PROBE = "probe"
MISSING_CODE = "missing_probe"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _param_number(params: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        if name in params:
            number = _number(params.get(name))
            if number is not None:
                return number
    return None


def _param_country(params: Mapping[str, Any]) -> str | None:
    for name in ("country", "country_code"):
        raw = params.get(name)
        if isinstance(raw, str):
            code = raw.strip().upper()
            if len(code) == 2 and code.isalpha():
                return code
    return None


def _missing_mode(config: ScoringConfig) -> str:
    return config.filters.missing


def _probe_gate(
    result: Any,
    config: ScoringConfig,
) -> tuple[str, list[str], list[str]]:
    """Classify the probe data. Returns (outcome, pending, filters_failed)."""
    if result is None:
        if _missing_mode(config) == "exclude":
            return "filtered", [], [MISSING_CODE]
        return "pending", [MISSING_PROBE], []
    status = str(getattr(result.status, "value", result.status))
    if status == ProbeStatus.SKIPPED.value:
        if _missing_mode(config) == "exclude":
            return "filtered", [], [MISSING_CODE]
        return "pending", [MISSING_PROBE], []
    if status in MEASURED_FAILURE_STATUSES:
        return "filtered", [], ["probe"]
    mode = str(getattr(result.probe_mode, "value", result.probe_mode))
    if config.filters.require_real_probe_success and mode != ProbeMode.REAL.value:
        return "filtered", [], ["probe_mode"]
    return "ok", [], []


def _critical_missing(
    kind: str, values: Mapping[str, Any], config: ScoringConfig
) -> list[str]:
    missing: list[str] = []
    if values.get("latency_ms") is None:
        missing.append("latency_ms")
    if values.get("speed_mb_s") is None:
        missing.append("speed_mb_s")
    if kind == "endpoint" and values.get("host_compatible") is None:
        missing.append("host_compatible")
    return missing


def _history_entry(
    history: Mapping[str, Mapping[int, HistorySummary]] | None,
    item_id: str,
    window_days: int,
) -> HistorySummary:
    entry = (history or {}).get(item_id) or {}
    summary = entry.get(window_days)
    if summary is not None:
        return summary
    return HistorySummary(
        item_id=item_id,
        window_days=window_days,
        min_samples=0,
    )


def _score_proxy(
    node: ProxyNode,
    result: ProxyProbeResult | None,
    history: Mapping[str, Mapping[int, HistorySummary]] | None,
    *,
    run_id: str,
    runner_id: str,
    profile: str,
    scoring: ScoringConfig,
    history_days: int,
) -> RankedProxy:
    outcome, pending, filters_failed = _probe_gate(result, scoring)
    latency = speed = None
    speed_unit = "MB/s"
    attempts = timeouts = 0
    probe_status = "missing"
    probe_mode = "unknown"
    observed_at: datetime | None = None
    if result is not None:
        probe_status = str(result.status.value)
        probe_mode = str(result.probe_mode.value)
        observed_at = result.measured_at
        attempts = int(result.attempts)
        timeouts = int(result.timeouts)
        speed_unit = str(result.speed_unit)
        latency = (
            float(result.total_latency_ms)
            if result.total_latency_ms is not None
            else None
        )
        if result.speed_mb_s is not None:
            speed = float(result.speed_mb_s)
        elif result.speed_mbps is not None:
            speed = float(result.speed_mbps) / 8.0
    risk = _param_number(node.params, "risk")
    country = _param_country(node.params)
    if outcome == "ok":
        if scoring.filters.allowed_countries and (
            country is None or country not in scoring.filters.allowed_countries
        ):
            filters_failed.append("country")
        if risk is not None and risk > scoring.filters.max_risk:
            filters_failed.append("risk")
        if latency is not None and latency > scoring.filters.max_latency_ms:
            filters_failed.append("latency")
        if speed is not None and speed < scoring.filters.min_speed_mb_s:
            filters_failed.append("speed")
    missing_values = {
        "latency_ms": latency,
        "speed_mb_s": speed,
        "host_compatible": None,
    }
    missing = _critical_missing("proxy", missing_values, scoring)
    if outcome == "ok" and not filters_failed and missing:
        if _missing_mode(scoring) == "exclude":
            filters_failed.extend(f"missing_{name}" for name in missing)
            pending = []
        else:
            pending = list(missing)
    summary = _history_entry(history, node.item_id, history_days)
    status = "ranked"
    if filters_failed:
        status = "filtered"
        pending = []
    elif pending:
        status = "pending"
    score = 0.0
    breakdown: dict[str, float] = {}
    notes: dict[str, Any] = {}
    if status == "ranked":
        loss = 100.0 * timeouts / attempts if attempts > 0 else None
        dims: dict[str, float | None] = {
            "latency": latency_score(latency, scoring.filters.max_latency_ms)
            if latency is not None
            else None,
            "speed": speed_score(speed, SPEED_REF_MB_S) if speed is not None else None,
            "purity": purity_score(risk) if risk is not None else None,
            "stability": stability_score(summary.availability_rate)
            if summary.availability_rate is not None
            else None,
            "loss": loss_score(loss) if loss is not None else None,
        }
        if summary.sample_count < scoring.filters.stability_min_samples:
            dims["stability"] = None
            notes["stability_samples_insufficient"] = True
            notes["stability_penalty"] = STABILITY_INSUFFICIENT_PENALTY
        weights = {
            "latency": scoring.weights.latency,
            "speed": scoring.weights.speed,
            "purity": scoring.weights.purity,
            "stability": scoring.weights.stability,
            "loss": scoring.weights.loss,
        }
        score, breakdown = weighted_score(weights, dims)
        if notes.get("stability_samples_insufficient"):
            score = max(0.0, score * STABILITY_INSUFFICIENT_PENALTY)
    return RankedProxy(
        item_id=node.item_id,
        status=status,
        rank=1 if status == "ranked" else 0,
        score=score,
        score_breakdown=breakdown,
        pending=pending,
        filters_failed=filters_failed,
        notes=notes,
        scoring_version=SCORING_VERSION,
        rule_snapshot={},
        runner_id=runner_id,
        probe_status=probe_status,
        probe_mode=probe_mode,
        observed_at=observed_at,
        sample_count=summary.sample_count,
        availability_rate=summary.availability_rate,
        latency_ms=latency,
        speed_mb_s=speed,
        speed_unit=speed_unit,
        loss_pct=100.0 * timeouts / attempts if attempts > 0 else None,
        risk=risk,
        country_code=country,
        protocol=node.protocol,
        source_ids=list(node.source_ids),
        remarks=node.remarks,
    )


def _score_endpoint(
    edge: EdgeEndpoint,
    result: EndpointProbeResult | None,
    history: Mapping[str, Mapping[int, HistorySummary]] | None,
    *,
    run_id: str,
    runner_id: str,
    profile: str,
    scoring: ScoringConfig,
    history_days: int,
) -> RankedEndpoint:
    outcome, pending, filters_failed = _probe_gate(result, scoring)
    latency = speed = loss = None
    host_compatible: bool | None = None
    speed_unit = "MB/s"
    probe_status = "missing"
    probe_mode = "unknown"
    observed_at: datetime | None = None
    if result is not None:
        probe_status = str(result.status.value)
        probe_mode = str(result.probe_mode.value)
        observed_at = result.measured_at
        speed_unit = str(result.speed_unit)
        latency = float(result.latency_ms) if result.latency_ms is not None else None
        speed = float(result.speed_mb_s) if result.speed_mb_s is not None else None
        loss = float(result.loss_pct) if result.loss_pct is not None else None
        host_compatible = result.host_compatible
    if outcome == "ok":
        if host_compatible is False:
            filters_failed.append("compatibility")
        if latency is not None and latency > scoring.filters.max_latency_ms:
            filters_failed.append("latency")
        if speed is not None and speed < scoring.filters.min_speed_mb_s:
            filters_failed.append("speed")
    missing_values = {
        "latency_ms": latency,
        "speed_mb_s": speed,
        "host_compatible": host_compatible,
    }
    missing = _critical_missing("endpoint", missing_values, scoring)
    if outcome == "ok" and not filters_failed and missing:
        if _missing_mode(scoring) == "exclude":
            filters_failed.extend(f"missing_{name}" for name in missing)
            pending = []
        else:
            pending = list(missing)
    summary = _history_entry(history, edge.item_id, history_days)
    status = "ranked"
    if filters_failed:
        status = "filtered"
        pending = []
    elif pending:
        status = "pending"
    score = 0.0
    breakdown: dict[str, float] = {}
    if status == "ranked":
        dims = {
            "compatibility": 1.0 if host_compatible is True else None,
            "latency": latency_score(latency, scoring.filters.max_latency_ms)
            if latency is not None
            else None,
            "speed": speed_score(speed, SPEED_REF_MB_S) if speed is not None else None,
            "loss": loss_score(loss) if loss is not None else None,
        }
        weights = {
            "compatibility": scoring.cf_weights.compatibility,
            "latency": scoring.cf_weights.latency,
            "speed": scoring.cf_weights.speed,
            "loss": scoring.cf_weights.loss,
        }
        score, breakdown = weighted_score(weights, dims)
    return RankedEndpoint(
        item_id=edge.item_id,
        status=status,
        rank=1 if status == "ranked" else 0,
        score=score,
        score_breakdown=breakdown,
        pending=pending,
        filters_failed=filters_failed,
        scoring_version=SCORING_VERSION,
        rule_snapshot={},
        runner_id=runner_id,
        probe_status=probe_status,
        probe_mode=probe_mode,
        observed_at=observed_at,
        sample_count=summary.sample_count,
        availability_rate=summary.availability_rate,
        latency_ms=latency,
        speed_mb_s=speed,
        speed_unit=speed_unit,
        loss_pct=loss,
        address=edge.address,
        port=edge.port,
        target_host=edge.target_host,
        tls=edge.tls,
        host_compatible=host_compatible,
        source_ids=list(edge.source_ids),
        remarks=edge.remarks,
    )


def _assign_ranks(items: list[Any]) -> None:
    ranked = [item for item in items if item.status == "ranked"]
    ranked.sort(
        key=lambda item: (
            -item.score,
            item.latency_ms if item.latency_ms is not None else math.inf,
            item.item_id,
        )
    )
    for position, item in enumerate(ranked, start=1):
        item.rank = position


def score_run(
    *,
    run_id: str,
    runner_id: str,
    profile: str,
    nodes: Sequence[ProxyNode],
    edges: Sequence[EdgeEndpoint],
    probe_results: Sequence[Any],
    proxy_history: Mapping[str, Mapping[int, HistorySummary]] | None = None,
    endpoint_history: Mapping[str, Mapping[int, HistorySummary]] | None = None,
    scoring: ScoringConfig,
    history_days: int,
    generated_at: datetime | None = None,
) -> ScoreReport:
    """Score and rank every entity against probe data, history and filters."""
    results: dict[str, Any] = {}
    issues: list[ScoreIssue] = []
    entity_ids = {node.item_id for node in nodes} | {edge.item_id for edge in edges}
    for result in probe_results:
        item_id = str(result.item_id)
        if item_id in results:
            continue
        results[item_id] = result
    for item_id in sorted({str(getattr(r, "item_id", "")) for r in probe_results}):
        if item_id and item_id not in entity_ids:
            kind = (
                "proxy_node"
                if isinstance(results.get(item_id), ProxyProbeResult)
                else "edge_endpoint"
            )
            issues.append(
                ScoreIssue(
                    item_id=item_id,
                    kind=kind,
                    code="probe_orphan",
                    message_redacted="probe result has no matching entity",
                )
            )
    proxies = [
        _score_proxy(
            node,
            results.get(node.item_id),
            proxy_history,
            run_id=run_id,
            runner_id=runner_id,
            profile=profile,
            scoring=scoring,
            history_days=history_days,
        )
        for node in nodes
    ]
    endpoints = [
        _score_endpoint(
            edge,
            results.get(edge.item_id),
            endpoint_history,
            run_id=run_id,
            runner_id=runner_id,
            profile=profile,
            scoring=scoring,
            history_days=history_days,
        )
        for edge in edges
    ]
    _assign_ranks(proxies)
    _assign_ranks(endpoints)
    counts = {
        "proxies": len(proxies),
        "endpoints": len(endpoints),
        "ranked": sum(1 for item in proxies if item.status == "ranked")
        + sum(1 for item in endpoints if item.status == "ranked"),
        "filtered": sum(1 for item in proxies if item.status == "filtered")
        + sum(1 for item in endpoints if item.status == "filtered"),
        "pending": sum(1 for item in proxies if item.status == "pending")
        + sum(1 for item in endpoints if item.status == "pending"),
        "issues": len(issues),
    }
    rule_snapshot: dict[str, Any] = {
        "scoring_version": SCORING_VERSION,
        "speed_ref_mb_s": SPEED_REF_MB_S,
        "stability_penalty": STABILITY_INSUFFICIENT_PENALTY,
        "history_days": int(history_days),
        "weights": {
            "latency": scoring.weights.latency,
            "speed": scoring.weights.speed,
            "purity": scoring.weights.purity,
            "stability": scoring.weights.stability,
            "loss": scoring.weights.loss,
        },
        "cf_weights": {
            "compatibility": scoring.cf_weights.compatibility,
            "latency": scoring.cf_weights.latency,
            "speed": scoring.cf_weights.speed,
            "loss": scoring.cf_weights.loss,
        },
        "filters": scoring.filters.model_dump(mode="json"),
    }
    for item in (*proxies, *endpoints):
        item.rule_snapshot = rule_snapshot
    if generated_at is not None:
        return ScoreReport(
            run_id=run_id,
            runner_id=runner_id,
            profile=profile,
            generated_at=generated_at,
            scoring_version=SCORING_VERSION,
            rule_snapshot=rule_snapshot,
            proxies=proxies,
            endpoints=endpoints,
            issues=issues,
            counts=counts,
        )
    return ScoreReport(
        run_id=run_id,
        runner_id=runner_id,
        profile=profile,
        scoring_version=SCORING_VERSION,
        rule_snapshot=rule_snapshot,
        proxies=proxies,
        endpoints=endpoints,
        issues=issues,
        counts=counts,
    )


__all__ = [
    "score_run",
]
