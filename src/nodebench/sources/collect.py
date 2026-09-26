from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nodebench.core.config import AppConfig
from nodebench.core.context import redact
from nodebench.core.schema import RawItem, SourceReport
from nodebench.sources.base import CollectOutcome, make_error
from nodebench.sources.local import collect_local

PENDING_SOURCES = ("subscriptions", "github", "cf")


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


def collect_all(config: AppConfig, base_dir: Path | None = None) -> CollectOutcome:
    """Run every enabled source adapter; one failing source never blocks the rest."""
    base = Path(base_dir) if base_dir is not None else Path.cwd()
    items: list[RawItem] = []
    reports: list[SourceReport] = []
    if config.sources.local.enabled:
        _run_adapter(
            "local",
            lambda: collect_local(config.sources.local, base),
            items,
            reports,
        )
    for name in PENDING_SOURCES:
        if getattr(config.sources, name).enabled:
            reports.append(
                _failed_report(
                    name,
                    "adapter_unavailable",
                    f"{name} source is enabled but its adapter is not implemented yet",
                )
            )
    return CollectOutcome(items=items, reports=reports)


__all__ = ["PENDING_SOURCES", "collect_all"]
