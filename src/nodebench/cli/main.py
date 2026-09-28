from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from nodebench.core.config import AppConfig, load_config
from nodebench.core.context import build_run_context, redact
from nodebench.core.errors import (
    ConfigError,
    ExportError,
    NodeBenchError,
    exit_code_for,
)
from nodebench.core.orchestrator import resolve_run_exit, run_pipeline
from nodebench.core.schema import RUN_ID_PATTERN, SCHEMA_VERSION
from nodebench.core.serialization import dumps_json, public_dump, write_json_atomic
from nodebench.core.stages import (
    export_artifacts,
    inspect_artifacts,
    score_artifacts,
    scored_path,
)
from nodebench.probes import (
    LEVEL_WARN,
    as_check,
    check_cf_prereqs,
    check_proxy_prereqs,
)
from nodebench.scheduler import service as scheduler_service

STUB_MESSAGE = "not implemented yet"
DOCTOR_MESSAGE = "core ready, probes available"
DOCTOR_MESSAGE_PENDING = "core ready, probes pending"
WRITE_PROBE_NAME = ".doctor-write-check.tmp"
BINARY_NAMES = ("clash", "gh")
CREDENTIAL_ENV_NAMES = ("NODEBENCH_GITHUB_TOKEN", "NODEBENCH_REPUTATION_API_KEY")


def _stub(args: argparse.Namespace) -> int:
    print(STUB_MESSAGE)
    return 0


def _project_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "config" / "default.yaml").is_file():
            return candidate
    cwd = Path.cwd()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "config" / "default.yaml").is_file():
            return candidate
    return here.parents[3]


def _load(
    args: argparse.Namespace,
    env: Mapping[str, str] | None = None,
    use_input: bool = True,
) -> AppConfig:
    root = _project_root()
    profile = getattr(args, "profile", None)
    profile_path = (
        root / "config" / "profiles" / f"{profile}.yaml" if profile else None
    )
    overrides: dict[str, Any] = {}
    input_path = getattr(args, "input", None) if use_input else None
    if input_path:
        overrides["sources.local.enabled"] = True
        overrides["sources.local.paths"] = [str(input_path)]
    output_dir = getattr(args, "output_dir", None)
    if output_dir:
        overrides["output_dir"] = str(output_dir)
    return load_config(
        root / "config" / "default.yaml",
        profile_path,
        env=env,
        cli_overrides=overrides or None,
    )


def _report_path(
    config: AppConfig, output_dir: str | None, run_id: str, dry_run: bool
) -> Path:
    base = Path(output_dir) if output_dir else Path(config.output_dir)
    if not base.is_absolute():
        base = _project_root() / base
    name = "dry-run-report.json" if dry_run else "run-report.json"
    return base / run_id / name


def _probe_results_path(
    config: AppConfig, output_dir: str | None, run_id: str
) -> Path:
    return _report_path(config, output_dir, run_id, False).with_name(
        "probe-results.json"
    )


def _model_dump(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="python")
    return value


def _write_probe_results(
    path: Path, run_id: str, generated_at: str, results: list
) -> Path:
    payload = public_dump(
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "generated_at": generated_at,
            "results": [_model_dump(result) for result in results],
        }
    )
    return write_json_atomic(path, payload)


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


def _error_text(err: BaseException) -> str:
    if isinstance(err, NodeBenchError):
        return redact(err.message)
    return redact(f"{type(err).__name__}: {err}")


def _check_output_dir(root: Path, config: AppConfig | None) -> tuple[str, str, str, str]:
    raw = config.output_dir if config is not None else "output"
    target = Path(raw) if raw else Path("output")
    if not target.is_absolute():
        target = root / target
    probe = target / WRITE_PROBE_NAME
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
    except OSError as err:
        return ("fail", "output", _error_text(err), f"grant write access to {target}")
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    return ("ok", "output", str(target), "")


def _doctor_checks(args: argparse.Namespace) -> list[tuple[str, str, str, str]]:
    checks: list[tuple[str, str, str, str]] = []
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info >= (3, 12):
        checks.append(("ok", "python", version, ""))
    else:
        checks.append(
            (
                "fail",
                "python",
                f"{version} is older than 3.12",
                "install python 3.12 or newer",
            )
        )
    root = _project_root()
    shipped: AppConfig | None = None
    try:
        shipped = _load(args, env={})
    except Exception as err:
        checks.append(
            (
                "fail",
                "config",
                _error_text(err),
                "repair config/default.yaml and config/sources.yaml",
            )
        )
    if shipped is not None:
        checks.append(("ok", "config", f"profile {shipped.profile} loads", ""))
    config: AppConfig | None = None
    try:
        config = _load(args)
    except Exception as err:
        checks.append(
            (
                "fail",
                "environment",
                _error_text(err),
                "unset or fix the NODEBENCH_* environment variables",
            )
        )
    if config is not None:
        checks.append(("ok", "environment", "effective config loads", ""))
    input_dir = root / "input"
    try:
        sample_files = [
            path
            for path in input_dir.rglob("*")
            if path.is_file() and not any(part.startswith(".") for part in path.parts)
        ]
    except OSError as err:
        checks.append(("fail", "input", _error_text(err), "check the input directory"))
    else:
        if sample_files:
            checks.append(("ok", "input", f"{len(sample_files)} sample files", ""))
        else:
            checks.append(
                ("fail", "input", "no readable sample files", "add samples under input/")
            )
    checks.append(_check_output_dir(root, config))
    if config is not None:
        checks.extend(
            as_check(report) for report in check_proxy_prereqs(config, root)
        )
        checks.extend(as_check(report) for report in check_cf_prereqs(config, root))
    for name in BINARY_NAMES:
        found = shutil.which(name)
        if found:
            checks.append(("ok", name, found, ""))
        else:
            checks.append(
                ("warn", name, "not found on PATH", f"install {name} and add it to PATH")
            )
    for name in CREDENTIAL_ENV_NAMES:
        state = "set" if os.environ.get(name) else "not set"
        checks.append(("info", name.lower(), state, ""))
    return checks


def _cmd_doctor(args: argparse.Namespace) -> int:
    try:
        checks = _doctor_checks(args)
    except Exception as err:
        print(f"[FAIL] doctor: {_error_text(err)} (fix: repair the installation)")
        return 2
    failures = 0
    for level, name, detail, fix in checks:
        line = f"[{level.upper()}] {name}: {detail}"
        if fix:
            line += f" (fix: {fix})"
        print(line)
        if level == "fail":
            failures += 1
    if failures:
        print(f"{failures} failing check(s)")
        return 2
    pending = any(level == LEVEL_WARN for level, _name, _detail, _fix in checks)
    print(DOCTOR_MESSAGE_PENDING if pending else DOCTOR_MESSAGE)
    return 0


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


def _require_run_id(args: argparse.Namespace) -> str:
    run_id = str(getattr(args, "run_id", None) or "")
    if not re.fullmatch(RUN_ID_PATTERN, run_id):
        raise ConfigError(
            code="run_id_required",
            message=(
                "--run-id is required and must look like "
                "20250101T120000Z-abcdef"
            ),
        )
    return run_id


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


SCHEDULER_ACTIONS = ("install", "status", "uninstall")
YES_HINT = "pass --yes to create"
UNINSTALL_YES_HINT = "pass --yes to uninstall"


def _is_interactive() -> bool:
    try:
        return bool(
            sys.stdin is not None
            and sys.stdout is not None
            and sys.stdin.isatty()
            and sys.stdout.isatty()
        )
    except (AttributeError, ValueError, OSError):
        return False


def _scheduler_settings(args: argparse.Namespace) -> tuple[str, str, str]:
    config = load_config(_project_root() / "config" / "default.yaml", None, env=os.environ)
    output_dir = str(config.output_dir or "output")
    default_time = str(getattr(config.scheduler, "default_time", "04:37"))
    time_value = getattr(args, "time", None) or default_time
    return str(time_value), output_dir, str(config.profile or "local")


def _join_argv(argv: Sequence[str]) -> str:
    parts: list[str] = []
    for token in argv:
        if " " in token and '"' not in token:
            parts.append(f'"{token}"')
        else:
            parts.append(token)
    return " ".join(parts)


def _print_scheduler_plan(result: scheduler_service.SchedulerResult) -> None:
    task = result.task or {}
    print(f"plan {result.action} scheduled task:")
    print(f"  name: {task.get('name') or result.name}")
    print(f"  profile: {task.get('profile') or result.profile}")
    print(f"  time: {task.get('time') or result.time}")
    print(f"  platform: {task.get('platform') or result.platform}")
    print(f"  command: {task.get('command', '')}")
    print(f"  log: {task.get('log_path', '')}")
    for command in result.commands:
        print(f"  run: {_join_argv(command)}")


def _print_text(text: str) -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        text = text.encode(encoding, "replace").decode(encoding, "replace")
    print(text)


def _print_scheduler_json(result: scheduler_service.SchedulerResult) -> None:
    _print_text(dumps_json(public_dump(result.model_dump())))


def _print_scheduler_messages(result: scheduler_service.SchedulerResult) -> None:
    for message in result.messages:
        _print_text(message)


def _confirm_or_hint(
    prompt: str,
    *,
    assume_yes: bool,
    json_mode: bool,
) -> tuple[bool, bool]:
    if assume_yes:
        return True, False
    if json_mode or not _is_interactive():
        return False, False
    answer = input(f"{prompt} [y/N] ").strip().lower()
    if answer in {"y", "yes"}:
        return True, False
    return False, True


def _scheduler_install(args: argparse.Namespace, json_mode: bool) -> int:
    profile = str(getattr(args, "profile", None) or "")
    if not profile:
        raise ConfigError(
            code="scheduler_profile_required",
            message="install needs --profile <name>",
        )
    time_value, output_dir, _default_profile = _scheduler_settings(args)
    options = {
        "profile": profile,
        "time_value": time_value,
        "name": getattr(args, "name", None),
        "output_dir": output_dir,
    }
    plan = scheduler_service.install(**options, dry_run=True)
    if not json_mode:
        _print_scheduler_plan(plan)
    if getattr(args, "dry_run", False):
        if json_mode:
            _print_scheduler_json(plan)
        else:
            _print_scheduler_messages(plan)
        return 0
    confirmed, declined = _confirm_or_hint(
        f"create scheduled task {plan.name}?",
        assume_yes=bool(getattr(args, "yes", False)),
        json_mode=json_mode,
    )
    if not confirmed:
        plan.messages = ["declined: scheduled task not created" if declined else YES_HINT]
        if json_mode:
            _print_scheduler_json(plan)
        else:
            _print_scheduler_messages(plan)
        return 0
    result = scheduler_service.install(**options, dry_run=False)
    if json_mode:
        _print_scheduler_json(result)
    else:
        _print_scheduler_messages(result)
    return 0


def _scheduler_status(args: argparse.Namespace, json_mode: bool) -> int:
    _time_value, output_dir, _default_profile = _scheduler_settings(args)
    result = scheduler_service.status(
        profile=getattr(args, "profile", None),
        name=getattr(args, "name", None),
        time_value=getattr(args, "time", None),
        output_dir=output_dir,
    )
    if json_mode:
        _print_scheduler_json(result)
        return 0
    for item in result.statuses:
        print(
            "task {0}: exists=yes next_run={1} last_run={2} last_result={3}".format(
                item.get("name"),
                item.get("next_run") or "-",
                item.get("last_run") or "-",
                item.get("last_result") or "-",
            )
        )
    _print_scheduler_messages(result)
    return 0


def _scheduler_uninstall(args: argparse.Namespace, json_mode: bool) -> int:
    time_value, output_dir, default_profile = _scheduler_settings(args)
    options = {
        "profile": getattr(args, "profile", None),
        "name": getattr(args, "name", None),
        "time_value": getattr(args, "time", None) or time_value,
        "display_profile": getattr(args, "profile", None) or default_profile,
        "output_dir": output_dir,
    }
    plan = scheduler_service.uninstall(**options, dry_run=True)
    if not json_mode:
        _print_scheduler_plan(plan)
    if getattr(args, "dry_run", False):
        if json_mode:
            _print_scheduler_json(plan)
        else:
            _print_scheduler_messages(plan)
        return 0
    confirmed, declined = _confirm_or_hint(
        f"remove scheduled task {plan.name}?",
        assume_yes=bool(getattr(args, "yes", False)),
        json_mode=json_mode,
    )
    if not confirmed:
        plan.messages = ["declined: scheduled task kept" if declined else UNINSTALL_YES_HINT]
        if json_mode:
            _print_scheduler_json(plan)
        else:
            _print_scheduler_messages(plan)
        return 0
    result = scheduler_service.uninstall(**options, dry_run=False)
    if json_mode:
        _print_scheduler_json(result)
    else:
        _print_scheduler_messages(result)
    return 0


def _cmd_scheduler(args: argparse.Namespace) -> int:
    action = getattr(args, "action", None)
    if action not in SCHEDULER_ACTIONS:
        print(
            "error: scheduler needs an action: install, status or uninstall",
            file=sys.stderr,
        )
        return 2
    json_mode = bool(getattr(args, "json_output", False))
    if action == "install":
        return _scheduler_install(args, json_mode)
    if action == "status":
        return _scheduler_status(args, json_mode)
    return _scheduler_uninstall(args, json_mode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nodebench",
        description=(
            "Bounded collection, probing and reporting of authorized "
            "proxy nodes and Cloudflare edge endpoints"
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="print a traceback for unexpected errors",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run the full pipeline")
    run_parser.add_argument("--profile", default=None, help="profile name")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check source scope and budget without probing",
    )
    run_parser.add_argument(
        "--run-id",
        default=None,
        help="reuse an existing run id; 仅用于读取/恢复指定 run，不用于重复测量",
    )
    run_parser.add_argument("--input", default=None, help="input file or directory")
    run_parser.add_argument(
        "--output-dir", default=None, help="directory that receives run reports"
    )
    run_parser.add_argument(
        "--strict",
        action="store_true",
        help="fail when probing is enabled but produces no real measurements",
    )
    run_parser.add_argument(
        "--no-publish",
        action="store_true",
        help="skip publishing output/latest after the export stage",
    )
    run_parser.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print a traceback for unexpected errors",
    )
    run_parser.set_defaults(func=_cmd_run)

    doctor_parser = subparsers.add_parser(
        "doctor", help="check python, binaries, credentials and config"
    )
    doctor_parser.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print a traceback for unexpected errors",
    )
    doctor_parser.set_defaults(func=_cmd_doctor)

    for name, handler in (
        ("collect", _cmd_collect),
        ("probe", _cmd_probe),
        ("inspect", _cmd_inspect),
        ("score", _cmd_score),
        ("export", _cmd_export),
    ):
        stage_parser = subparsers.add_parser(name, help=f"{name} stage")
        stage_parser.add_argument(
            "--run-id",
            default=None,
            help="upstream run id; 仅用于读取/恢复指定 run，不用于重复测量",
        )
        stage_parser.add_argument("--input", default=None, help="upstream artifact")
        if name in ("collect", "probe"):
            stage_parser.add_argument("--profile", default=None, help="profile name")
            stage_parser.add_argument(
                "--debug",
                action="store_true",
                default=argparse.SUPPRESS,
                help="print a traceback for unexpected errors",
            )
        if name == "probe":
            stage_parser.add_argument(
                "--output-dir",
                default=None,
                help="directory that receives probe reports",
            )
        if name in ("score", "export", "inspect"):
            stage_parser.add_argument(
                "--output-dir",
                default=None,
                help="directory that holds run artifacts",
            )
        stage_parser.set_defaults(func=handler)

    scheduler_parser = subparsers.add_parser(
        "scheduler", help="manage system scheduled tasks"
    )
    scheduler_parser.add_argument(
        "action",
        nargs="?",
        choices=("install", "status", "uninstall"),
        default=None,
        help="scheduler action",
    )
    scheduler_parser.add_argument("--profile", default=None, help="profile name")
    scheduler_parser.add_argument("--time", default=None, help="local time HH:MM")
    scheduler_parser.add_argument(
        "--name",
        default=None,
        help="scheduled task name, always nodebench-*",
    )
    scheduler_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without touching the system scheduler",
    )
    scheduler_parser.add_argument(
        "--yes",
        action="store_true",
        help="execute without an interactive confirmation",
    )
    scheduler_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="print one machine readable json document",
    )
    scheduler_parser.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print a traceback for unexpected errors",
    )
    scheduler_parser.set_defaults(func=_cmd_scheduler)
    return parser


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as err:
        code = err.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2
    handler = getattr(args, "func", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return 2
    debug = bool(getattr(args, "debug", False))
    try:
        return int(handler(args))
    except NodeBenchError as err:
        print(
            f"error [stage={err.stage}, code={err.code}]: {redact(err.message)}",
            file=sys.stderr,
        )
        return exit_code_for(err)
    except Exception as err:
        if debug:
            traceback.print_exc()
        print(f"error: {type(err).__name__}: {redact(str(err))}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(argv)


if __name__ == "__main__":
    sys.exit(main())
