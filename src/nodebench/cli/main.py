from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from nodebench.core.errors import NodeBenchError, exit_code_for

STUB_MESSAGE = "not implemented yet"
DOCTOR_MESSAGE = "core ready, adapters pending"


def _stub(args: argparse.Namespace) -> int:
    print(STUB_MESSAGE)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    return _stub(args)


def _cmd_doctor(args: argparse.Namespace) -> int:
    print(DOCTOR_MESSAGE)
    return 2


def _cmd_collect(args: argparse.Namespace) -> int:
    return _stub(args)


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
    run_parser.set_defaults(func=_cmd_run)

    doctor_parser = subparsers.add_parser(
        "doctor", help="check python, binaries, credentials and config"
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
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler = getattr(args, "func", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return 2
    try:
        return int(handler(args))
    except NodeBenchError as err:
        print(str(err), file=sys.stderr)
        return exit_code_for(err)


def main(argv: Sequence[str] | None = None) -> None:
    sys.exit(run_cli(argv))


if __name__ == "__main__":
    main()
