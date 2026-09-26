from __future__ import annotations

import argparse
import os
import shutil
import sys
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from nodebench.core.config import AppConfig, load_config
from nodebench.core.context import build_run_context, redact
from nodebench.core.errors import NodeBenchError, exit_code_for
from nodebench.core.orchestrator import run_pipeline
from nodebench.core.serialization import write_json_atomic

STUB_MESSAGE = "not implemented yet"
DOCTOR_MESSAGE = "core ready, adapters pending"
WRITE_PROBE_NAME = ".doctor-write-check.tmp"
BINARY_NAMES = ("mihomo", "clash", "gh")
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
    args: argparse.Namespace, env: Mapping[str, str] | None = None
) -> AppConfig:
    root = _project_root()
    profile = getattr(args, "profile", None)
    profile_path = (
        root / "config" / "profiles" / f"{profile}.yaml" if profile else None
    )
    overrides: dict[str, Any] = {}
    input_path = getattr(args, "input", None)
    if input_path:
        overrides["sources.local.enabled"] = True
        overrides["sources.local.paths"] = [str(input_path)]
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


def _cmd_run(args: argparse.Namespace) -> int:
    config = _load(args)
    ctx = build_run_context(config, args)
    result = run_pipeline(config, ctx, dry_run=bool(args.dry_run))
    path = _report_path(config, args.output_dir, ctx.run_id, bool(args.dry_run))
    write_json_atomic(path, result)
    print(f"[{ctx.run_id}] status={result['status']} report={path}")
    return 3 if result["status"] == "failed" else 0


def _cmd_collect(args: argparse.Namespace) -> int:
    config = _load(args)
    ctx = build_run_context(config, args)
    result = run_pipeline(config, ctx, dry_run=False)
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
    return 3 if result["status"] == "failed" else 0


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
        cf_enabled = config.sources.cf.enabled or config.probe.cf.enabled
        target_host = (config.probe.cf.target_host or "").strip()
        if not cf_enabled:
            checks.append(("ok", "cf_target", "cf probing disabled", ""))
        elif target_host:
            checks.append(("ok", "cf_target", target_host, ""))
        else:
            checks.append(
                (
                    "fail",
                    "cf_target",
                    "probe.cf.target_host is empty while cf is enabled",
                    "set probe.cf.target_host or disable cf",
                )
            )
        speedtest_url = (config.probe.proxy.speedtest_url or "").strip()
        if speedtest_url:
            checks.append(("ok", "probe_url", speedtest_url, ""))
        else:
            checks.append(
                (
                    "warn",
                    "probe_url",
                    "probe.proxy.speedtest_url is empty; probing stays pending",
                    "set probe.proxy.speedtest_url before enabling probing",
                )
            )
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
    print(DOCTOR_MESSAGE)
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    return _stub(args)


def _cmd_inspect(args: argparse.Namespace) -> int:
    return _stub(args)


def _cmd_score(args: argparse.Namespace) -> int:
    return _stub(args)


def _cmd_export(args: argparse.Namespace) -> int:
    return _stub(args)


def _cmd_scheduler(args: argparse.Namespace) -> int:
    return _stub(args)


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
    run_parser.add_argument("--run-id", default=None, help="reuse an existing run id")
    run_parser.add_argument("--input", default=None, help="input file or directory")
    run_parser.add_argument(
        "--output-dir", default=None, help="directory that receives run reports"
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
        stage_parser.add_argument("--run-id", default=None, help="upstream run id")
        stage_parser.add_argument("--input", default=None, help="upstream artifact")
        if name == "collect":
            stage_parser.add_argument("--profile", default=None, help="profile name")
            stage_parser.add_argument(
                "--debug",
                action="store_true",
                default=argparse.SUPPRESS,
                help="print a traceback for unexpected errors",
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
