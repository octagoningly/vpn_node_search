from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections.abc import Sequence

from nodebench.cli.commands_artifacts import (
    _cmd_export,
    _cmd_inspect,
    _cmd_publish,
    _cmd_score,
)
from nodebench.cli.commands_doctor import _cmd_doctor
from nodebench.cli.commands_pipeline import _cmd_collect, _cmd_probe, _cmd_run
from nodebench.cli.commands_scheduler import _cmd_scheduler
from nodebench.cli.common import _project_root, _stub
from nodebench.core.context import redact
from nodebench.core.errors import NodeBenchError, exit_code_for


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
        "--import-candidates",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "import a sorted/measured ADDAPI txt or ADDCSV csv list "
            "as source_id=imported CF candidates (repeatable)"
        ),
    )
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
        if name == "collect":
            stage_parser.add_argument(
                "--import-candidates",
                action="append",
                default=None,
                metavar="PATH",
                help=(
                    "import a sorted/measured ADDAPI txt or ADDCSV csv list "
                    "as source_id=imported CF candidates (repeatable)"
                ),
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

    publish_parser = subparsers.add_parser(
        "publish",
        help="publish approved public results for an existing run",
    )
    publish_parser.add_argument(
        "--run-id",
        default=None,
        help="run whose export/ directory is published to output/latest",
    )
    publish_parser.add_argument(
        "--profile",
        default=None,
        help="profile name (publish gates and upload come from this profile)",
    )
    publish_parser.add_argument(
        "--output-dir",
        default=None,
        help="directory that holds run artifacts",
    )
    publish_parser.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print a traceback for unexpected errors",
    )
    publish_parser.set_defaults(func=_cmd_publish)

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


def _load_local_env() -> None:
    """Load key=value pairs from .env in the project root (never committed)."""
    if "pytest" in sys.modules:
        return
    root = _project_root()
    path = root / ".env"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and value and name not in os.environ:
            os.environ[name] = value


def run_cli(argv: Sequence[str] | None = None) -> int:
    _load_local_env()
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
