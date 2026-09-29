from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nodebench.core.config import AppConfig, load_config
from nodebench.core.context import redact
from nodebench.core.errors import ConfigError, NodeBenchError
from nodebench.core.schema import RUN_ID_PATTERN, SCHEMA_VERSION
from nodebench.core.serialization import dumps_json, public_dump, write_json_atomic

if TYPE_CHECKING:
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
    import_files = getattr(args, "import_candidates", None)
    if import_files:
        overrides["sources.candidate_import.enabled"] = True
        overrides["sources.candidate_import.files"] = [str(item) for item in import_files]
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



def _error_text(err: BaseException) -> str:
    if isinstance(err, NodeBenchError):
        return redact(err.message)
    return redact(f"{type(err).__name__}: {err}")


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


