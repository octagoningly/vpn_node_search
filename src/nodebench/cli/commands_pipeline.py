from __future__ import annotations

import argparse

from nodebench.cli.common import (
    _load,
    _probe_results_path,
    _report_path,
    _write_probe_results,
)
from nodebench.core.context import build_run_context
from nodebench.core.serialization import write_json_atomic
from nodebench.pipeline.orchestrator import resolve_run_exit, run_pipeline


def _cmd_run(args: argparse.Namespace) -> int:
    config = _load(args)
    ctx = build_run_context(config, args)
    results: list = []
    dry_run = bool(args.dry_run)
    result = run_pipeline(
        config,
        ctx,
        dry_run=dry_run,
        probe_sink=results,
        post_stages=not dry_run,
        allow_publish=not bool(getattr(args, "no_publish", False)),
    )
    path = _report_path(config, args.output_dir, ctx.run_id, dry_run)
    write_json_atomic(path, result)
    if results and not dry_run:
        probe_path = _write_probe_results(
            _probe_results_path(config, args.output_dir, ctx.run_id),
            ctx.run_id,
            str(result["generated_at"]),
            results,
        )
        print(f"[{ctx.run_id}] probe_results={probe_path}")
    print(f"[{ctx.run_id}] status={result['status']} report={path}")
    return resolve_run_exit(
        run_status=str(result["status"]),
        probe=result.get("probe"),
        probe_results=results or None,
        cf_enabled=bool(config.probe.cf.enabled),
        target_host=str(config.probe.cf.target_host or ""),
        strict=bool(getattr(args, "strict", False)),
        stages=result.get("stages"),
    )


def _cmd_collect(args: argparse.Namespace) -> int:
    config = _load(args)
    ctx = build_run_context(config, args)
    result = run_pipeline(config, ctx, dry_run=False, run_probes=False)
    for report in result["source_reports"]:
        print(
            "source {0}: ok={1} fetched={2} errors={3}".format(
                report["source_id"],
                report["ok"],
                report["fetched"],
                len(report["errors"]),
            )
        )
    for line in result["diagnostics"]:
        print(f"diagnostic {line}")
    counts = result["counts"]
    print(
        "raw_items={0} proxy_nodes={1} edge_endpoints={2} issues={3} status={4}".format(
            counts["raw_items"],
            counts["proxy_nodes"],
            counts["edge_endpoints"],
            counts["parse_issues"],
            result["status"],
        )
    )
    return resolve_run_exit(
        run_status=str(result["status"]),
        probe=result.get("probe"),
        cf_enabled=bool(config.probe.cf.enabled),
        target_host=str(config.probe.cf.target_host or ""),
    )


def _cmd_probe(args: argparse.Namespace) -> int:
    config = _load(args)
    ctx = build_run_context(config, args)
    results: list = []
    result = run_pipeline(config, ctx, dry_run=False, probe_sink=results)
    probe = result.get("probe") or {}
    for kind in ("proxy", "cf"):
        node = probe.get(kind) or {}
        print(
            "probe {0}: mode={1} backend={2} attempted={3} ok={4} "
            "usable_real={5} skipped={6} reason={7}".format(
                kind,
                node.get("mode"),
                node.get("backend"),
                node.get("attempted"),
                node.get("ok"),
                node.get("usable_real"),
                node.get("skipped"),
                node.get("skipped_reason"),
            )
        )
    if results:
        probe_path = _write_probe_results(
            _probe_results_path(config, getattr(args, "output_dir", None), ctx.run_id),
            ctx.run_id,
            str(result["generated_at"]),
            results,
        )
        print(f"[{ctx.run_id}] probe_results={probe_path}")
    print(f"[{ctx.run_id}] status={result['status']}")
    return resolve_run_exit(
        run_status=str(result["status"]),
        probe=probe,
        probe_results=results or None,
        cf_enabled=bool(config.probe.cf.enabled),
        target_host=str(config.probe.cf.target_host or ""),
    )
