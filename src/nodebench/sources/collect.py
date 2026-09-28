from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from nodebench.core.config import AppConfig
from nodebench.core.context import redact
from nodebench.core.schema import RawItem, SourceReport
from nodebench.sources.base import CollectOutcome, make_error, resolve_base_dir
from nodebench.sources.cf import collect_cf
from nodebench.sources.github import collect_github
from nodebench.sources.local import collect_local
from nodebench.sources.subscriptions import collect_subscriptions


def _failed_report(source_id: str, code: str, message: str) -> SourceReport:
    return SourceReport(
        source_id=source_id,
        ok=False,
        errors=[make_error(code, message)],
    )


def _run_adapter(
    source_id: str,
    adapter: Callable[[], CollectOutcome],
    items: list[RawItem],
    reports: list[SourceReport],
) -> None:
    try:
        outcome = adapter()
    except Exception as err:
        reports.append(
            _failed_report(source_id, "source_error", redact(f"{type(err).__name__}: {err}"))
        )
        return
    items.extend(outcome.items)
    reports.extend(outcome.reports)


def _cf_outcome(config: AppConfig, base: Path) -> CollectOutcome:
    cf_items, cf_reports = collect_cf(config, base, base_dir=base)
    return CollectOutcome(items=cf_items, reports=cf_reports)


def collect_all(
    config: AppConfig, ctx: Any = None, base_dir: str | Path | None = None
) -> CollectOutcome:
    """Run every enabled source adapter; one failing source never blocks the rest."""
    base = resolve_base_dir(ctx, base_dir)
    items: list[RawItem] = []
    reports: list[SourceReport] = []
    if config.sources.local.enabled:
        _run_adapter(
            "local",
            lambda: collect_local(config.sources.local, base),
            items,
            reports,
        )
    if config.sources.cf.enabled:
        _run_adapter(
            "cf",
            lambda: _cf_outcome(config, base),
            items,
            reports,
        )
    if config.sources.subscriptions.enabled:
        _run_adapter(
            "subscriptions",
            lambda: collect_subscriptions(config.sources.subscriptions, base),
            items,
            reports,
        )
    if config.sources.github.enabled:
        _run_adapter(
            "github",
            lambda: collect_github(
                config.sources.github,
                base,
                secrets=dict(config.secrets) if config.secrets else None,
            ),
            items,
            reports,
        )
    return CollectOutcome(items=items, reports=reports)


__all__ = ["collect_all"]
