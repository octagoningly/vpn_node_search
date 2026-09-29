from __future__ import annotations

import csv
import io
import locale
import subprocess
import sys
from pathlib import Path

from nodebench.core.errors import ConfigError, NodeBenchError
from nodebench.scheduler.base import (
    MISSED_RUN_HINT,
    TASK_PREFIX,
    CommandResult,
    SchedulerBackend,
    ScheduledTask,
    TaskStatus,
    ensure_owned_name,
)

SCHTASKS = "schtasks"
MAX_TR_CHARS = 261
MUTATING_SWITCHES = ("/CREATE", "/CHANGE", "/DELETE", "/RUN", "/END", "/FREEZE", "/PAUSE")
FOREIGN_HINT = "only tasks named nodebench-* belong to this project"


class SchtasksError(NodeBenchError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(stage="scheduler", code=code, message=message)


def _quote(value: str) -> str:
    return '"' + str(value).replace('"', "'") + '"'


def _wrap_command(task: ScheduledTask) -> str:
    executable = sys.executable or "python"
    root = Path(task.root) if task.root else Path.cwd()
    # Task Scheduler starts in System32 — without an explicit cwd the
    # relative candidates/*.txt and config paths all miss.
    inner = (
        f'cd /d "{root}" && '
        f'"{executable}" -m nodebench.cli.main run --profile "{task.profile}"'
        f' >> "{task.log_path}" 2>&1'
    )
    return 'cmd.exe /c "' + inner + '"'


def build_schtasks_argv(task: ScheduledTask) -> list[str]:
    wrapped = _wrap_command(task)
    if len(wrapped) > MAX_TR_CHARS:
        raise ConfigError(
            code="scheduler_command_too_long",
            message=(
                f"scheduled task command too long ({len(wrapped)} chars, "
                f"limit {MAX_TR_CHARS}): {wrapped}"
            ),
        )
    hour, minute = task.time.split(":")
    return [
        SCHTASKS,
        "/Create",
        "/TN",
        task.name,
        "/TR",
        wrapped,
        "/SC",
        "DAILY",
        "/ST",
        task.time,
        "/F",
    ]


def guard_argv(argv: list[str]) -> None:
    task_name: str | None = None
    for index, token in enumerate(argv):
        if token.upper() == "/TN" and index + 1 < len(argv):
            task_name = argv[index + 1]
    if any(token.upper() in MUTATING_SWITCHES for token in argv):
        if task_name is None:
            raise ConfigError(
                code="scheduler_safety_gate",
                message="mutating schtasks switch without /TN <name> is refused",
            )
        if not task_name.startswith(f"{TASK_PREFIX}-"):
            raise ConfigError(
                code="scheduler_safety_gate",
                message=(
                    f"refusing to touch scheduled task {task_name!r}: {FOREIGN_HINT}"
                ),
            )


def decode_output(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    encodings = ["utf-8", locale.getpreferredencoding(False), "gbk"]
    for encoding in encodings:
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def clean_name(value: object) -> str:
    text = str(value if value is not None else "").strip()
    while text.startswith("\\"):
        text = text[1:]
    return text.strip()


def run_command(argv: list[str], *, execute: bool) -> CommandResult:
    guard_argv(argv)
    if not execute:
        return CommandResult(argv=argv, executed=False)
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SchtasksError(
            code="schtasks_unavailable",
            message=f"cannot execute {argv[0]!r}: {exc}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SchtasksError(
            code="schtasks_timeout",
            message=f"{argv[0]} timed out: {' '.join(argv)}",
        ) from exc
    return CommandResult(
        argv=argv,
        executed=True,
        returncode=completed.returncode,
        stdout=decode_output(completed.stdout),
        stderr=decode_output(completed.stderr),
    )


KEY_ALIASES = {
    "taskname": "name",
    "task name": "name",
    "任务名": "name",
    "名称": "name",
    "next run time": "next_run",
    "next run": "next_run",
    "下次运行时间": "next_run",
    "last run time": "last_run",
    "last run": "last_run",
    "上次运行时间": "last_run",
    "last result": "last_result",
    "上次结果": "last_result",
    "结果": "last_result",
}


def _looks_like_csv(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return "," in stripped and '"' in stripped
    return False


CANONICAL_FIELDS = {"name", "next_run", "last_run", "last_result"}


def _status_from_record(record: dict) -> TaskStatus:
    status = TaskStatus()
    for key, value in record.items():
        raw = str(key).strip().lower()
        field = raw if raw in CANONICAL_FIELDS else KEY_ALIASES.get(raw)
        text = str(value).strip()
        if field == "name" and not status.name:
            status.name = clean_name(text)
        elif field == "next_run" and not status.next_run:
            status.next_run = text or None
        elif field == "last_run" and not status.last_run:
            status.last_run = text or None
        elif field == "last_result" and not status.last_result:
            status.last_result = text or None
    if not status.name:
        match = next(
            (
                value
                for value in record.values()
                if clean_name(value).startswith(f"{TASK_PREFIX}-")
            ),
            "",
        )
        status.name = clean_name(match)
    status.exists = status.name.startswith(f"{TASK_PREFIX}-")
    return status


def parse_query_csv(text: str) -> list[TaskStatus]:
    statuses: list[TaskStatus] = []
    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    if not rows:
        return statuses
    if len(rows) == 1 and not any(len(row) > 1 for row in rows):
        return parse_query_list(text)
    header = [cell.strip().lower() for cell in rows[0]]
    if len(rows) == 1 or len(header) < 2:
        return parse_query_list(text)
    for row in rows[1:]:
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        status = _status_from_record(dict(zip(header, row)))
        if status.exists:
            statuses.append(status)
    return statuses


def parse_query_list(text: str) -> list[TaskStatus]:
    statuses: list[TaskStatus] = []
    record: dict = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        field = KEY_ALIASES.get(key.strip().lower())
        if field == "name":
            if record:
                status = _status_from_record(record)
                if status.exists:
                    statuses.append(status)
            record = {"taskname": value.strip()}
        elif field:
            record[field] = value.strip()
    if record:
        status = _status_from_record(record)
        if status.exists:
            statuses.append(status)
    if statuses:
        return statuses
    for token in text.replace(",", " ").split():
        cleaned = clean_name(token)
        if cleaned.startswith(f"{TASK_PREFIX}-"):
            statuses.append(TaskStatus(exists=True, name=cleaned))
    return statuses


def parse_query(text: str) -> list[TaskStatus]:
    if _looks_like_csv(text):
        return parse_query_csv(text)
    return parse_query_list(text)


def parse_command_excerpt(text: str) -> str:
    for line in text.splitlines():
        if "nodebench" in line.lower() and line.strip().startswith("cmd.exe"):
            return line.strip()
        if "nodebench" in line.lower():
            return line.strip()
    return ""


class SchtasksBackend(SchedulerBackend):
    platform = "windows"

    def __init__(self, runner=None) -> None:
        self._runner = runner or run_command

    def plan(self, task: ScheduledTask, action: str = "install") -> list[list[str]]:
        ensure_owned_name(task.name)
        if action == "uninstall":
            return [[SCHTASKS, "/Delete", "/TN", task.name, "/F"]]
        if action == "status":
            return self.query_plan(task.name)
        return [build_schtasks_argv(task)]

    def query_plan(self, name: str | None = None) -> list[list[str]]:
        if name:
            ensure_owned_name(name)
            return [[SCHTASKS, "/Query", "/TN", name, "/FO", "LIST", "/V"]]
        return [[SCHTASKS, "/Query", "/FO", "CSV", "/V"]]

    def run(self, commands: list[list[str]], *, execute: bool) -> list[CommandResult]:
        return [self._runner(command, execute=execute) for command in commands]

    def parse_query(self, text: str) -> list[TaskStatus]:
        return parse_query(text)

    def missed_run_hint(self) -> str:
        return MISSED_RUN_HINT

    def diagnose(self, result: CommandResult) -> str | None:
        if not result.executed or result.returncode in (None, 0):
            return None
        output = result.output.strip()
        lowered = output.lower()
        if "cannot find" in lowered or "找不到" in output or "system cannot find" in lowered:
            return f"no scheduled task for this project: {result.argv}"
        if "access is denied" in lowered or "拒绝访问" in output:
            return f"access denied running {' '.join(result.argv)}: {output}"
        return f"command failed ({result.returncode}): {' '.join(result.argv)}\n{output}"


def render_plan(argv: list[str]) -> str:
    return " ".join(_quote(token) if " " in token else token for token in argv)


def first_failure(results: list[CommandResult]) -> str | None:
    for result in results:
        if result.executed and result.returncode not in (None, 0):
            output = result.output.strip()
            return (
                f"command failed ({result.returncode}): "
                f"{' '.join(result.argv)}\n{output}"
            )
    return None
