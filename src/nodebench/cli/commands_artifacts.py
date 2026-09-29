from __future__ import annotations

import argparse
from collections.abc import Mapping

from nodebench.cli.common import _load, _require_run_id
from nodebench.core.errors import ExportError, PublishError
from nodebench.pipeline.stages import (
    export_artifacts,
    inspect_artifacts,
    publish_artifacts,
    score_artifacts,
    scored_path,
)


def _cmd_inspect(args: argparse.Namespace) -> int:
    """Generate and display exit/Geo/ASN/ISP/reputation intelligence for a run."""
    config = _load(args, use_input=False)
    run_id = _require_run_id(args)
    summary = inspect_artifacts(config, run_id)
    payload = summary.get("report_payload") or {}
    counts = payload.get("counts") or {}
    entries = payload.get("entries") or []
    reputations = {
        str(item.get("item_id") or ""): item
        for item in (payload.get("reputations") or [])
        if isinstance(item, Mapping)
    }
    print(f"Run ID: {run_id}")
    print(
        "  items={0} exit_ok={1} exit_unknown={2} reputation_ok={3}".format(
            counts.get("items", len(entries)),
            counts.get("exit_ok", 0),
            counts.get("exit_unknown", 0),
            counts.get("reputation_ok", 0),
        )
    )
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        item_id = str(entry.get("item_id") or "")
        exit_ip = str(entry.get("exit_ip") or "unknown")
        country = str(entry.get("country_code") or "unknown")
        asn = str(entry.get("asn") or "unknown")
        isp = str(entry.get("isp") or "unknown")
        exit_status = str(entry.get("status") or "unknown")
        print(f"  [{item_id}] exit_ip={exit_ip} status={exit_status}")
        print(f"    Geo: country={country} asn={asn} isp={isp}")
        snapshot = reputations.get(item_id) or {}
        risk = snapshot.get("risk")
        risk_text = "unknown" if risk is None else f"{risk}"
        print(
            "    Reputation: provider={0} risk={1} level={2} status={3}".format(
                snapshot.get("provider") or "unknown",
                risk_text,
                snapshot.get("risk_level") or "unknown",
                snapshot.get("status") or "unknown",
            )
        )
    report = summary.get("report") or ""
    print(f"[{run_id}] intelligence_report={report}")
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    config = _load(args, use_input=False)
    run_id = _require_run_id(args)
    score_report = score_artifacts(config, run_id)
    counts = score_report.counts
    print(
        "[{0}] score ranked={1} filtered={2} pending={3} issues={4} report={5}".format(
            run_id,
            counts.get("ranked"),
            counts.get("filtered"),
            counts.get("pending"),
            len(score_report.issues),
            scored_path(config, run_id),
        )
    )
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    config = _load(args, use_input=False)
    run_id = _require_run_id(args)
    outcome = export_artifacts(config, run_id)
    print(
        "[{0}] export status={1} files={2} proxies={3} endpoints={4} dir={5}".format(
            run_id,
            outcome.status,
            len(outcome.files),
            outcome.counts.get("proxies"),
            outcome.counts.get("endpoints"),
            outcome.directory,
        )
    )
    if outcome.status != "ok":
        raise ExportError(
            code=outcome.errors[0] if outcome.errors else "export_failed",
            message="export stage failed",
        )
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    """Release an existing export through local gates and optional upload."""
    config = _load(args, use_input=False)
    run_id = _require_run_id(args)
    result = publish_artifacts(config, run_id)
    files = result.get("files") or []
    print(
        "[{0}] publish status={1} files={2} path={3}".format(
            run_id,
            result.get("status"),
            len(files),
            result.get("path") or "-",
        )
    )
    for name in files:
        print(f"[{run_id}] published={name}")
    for url in result.get("public_urls") or []:
        print(f"[{run_id}] public_url={url}")
    status = str(result.get("status") or "")
    if status in {"ok", "skipped"}:
        if status == "skipped":
            print(f"[{run_id}] publish skipped: {result.get('reason') or 'disabled'}")
        return 0
    reason = str(result.get("reason") or "publish_failed")
    errors = result.get("errors") or []
    detail = str(errors[0]) if errors else reason
    raise PublishError(code=reason, message=f"publish stage failed: {detail}")
