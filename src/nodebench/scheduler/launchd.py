from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

from nodebench.core.errors import NodeBenchError
from nodebench.scheduler.base import (
    TASK_PREFIX,
    CommandResult,
    SchedulerBackend,
    ScheduledTask,
    TaskStatus,
    ensure_owned_name,
)

LABEL_PREFIX = "com.nodebench"


class LaunchdError(NodeBenchError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(stage="scheduler", code=code, message=message)


def launchd_label(task: ScheduledTask) -> str:
    return f"{LABEL_PREFIX}.{task.name}"


def launchd_plist_path(task: ScheduledTask) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}.{task.name}.plist"


def plist_payload(task: ScheduledTask) -> dict:
    hour, minute = (int(part) for part in task.time.split(":"))
    executable = sys.executable or "python"
    return {
        "Label": launchd_label(task),
        "ProgramArguments": [
            executable,
            "-m",
            "nodebench.cli.main",
            "run",
            "--profile",
            task.profile,
        ],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "WorkingDirectory": task.root,
        "StandardOutPath": task.log_path,
        "StandardErrorPath": task.log_path,
    }


def render_plist(task: ScheduledTask) -> str:
    data = plistlib.dumps(plist_payload(task), fmt=plistlib.FMT_XML, sort_keys=True)
    return data.decode("utf-8")


def render_launch_agents(task: ScheduledTask) -> list[tuple[str, str]]:
    path = launchd_plist_path(task)
    return [(str(path), render_plist(task))]


def run_command(argv: list[str], *, execute: bool) -> CommandResult:
    if not execute:
        return CommandResult(argv=argv, executed=False)
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except FileNotFoundError as exc:
        raise LaunchdError(
            code="launchd_unavailable",
            message=f"cannot execute {argv[0]!r}: {exc}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise LaunchdError(
            code="launchd_timeout",
            message=f"{argv[0]} timed out: {' '.join(argv)}",
        ) from exc
    return CommandResult(
        argv=argv,
        executed=True,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def parse_launchctl_list(text: str) -> list[TaskStatus]:
    statuses: list[TaskStatus] = []
    for line in text.splitlines():
        token = line.strip()
        if token.startswith(f"{LABEL_PREFIX}."):
            label = token.split(None, 1)[0]
            statuses.append(TaskStatus(exists=True, name=label_from(label)))
            continue
        if token.startswith(f"{TASK_PREFIX}-"):
            statuses.append(TaskStatus(exists=True, name=token))
    return statuses


def label_from(label: str) -> str:
    prefix = f"{LABEL_PREFIX}."
    if label.startswith(prefix):
        return label[len(prefix):]
    return label


def launchd_name(name: str) -> str:
    ensure_owned_name(name)
    return f"{LABEL_PREFIX}.{name}"


class LaunchdBackend(SchedulerBackend):
    platform = "macos"

    def __init__(self, runner=None) -> None:
        self._runner = runner or run_command

    def plan(self, task: ScheduledTask, action: str = "install") -> list[list[str]]:
        ensure_owned_name(task.name)
        if action == "uninstall":
            return [
                ["launchctl", "bootout", f"gui/{_uid()}/{launchd_label(task)}"],
            ]
        if action == "status":
            return self.query_plan(task.name)
        return [
            ["launchctl", "bootstrap", f"gui/{_uid()}", launchd_plist_path(task)],
        ]

    def query_plan(self, name: str | None = None) -> list[list[str]]:
        if name:
            return [["launchctl", "list", launchd_name(name)]]
        return [["launchctl", "list"]]

    def run(self, commands: list[list[str]], *, execute: bool) -> list[CommandResult]:
        return [self._runner(command, execute=execute) for command in commands]

    def parse_query(self, text: str) -> list[TaskStatus]:
        return parse_launchctl_list(text)

    def render(self, task: ScheduledTask) -> list[tuple[str, str]]:
        return render_launch_agents(task)

    def write(self, files: list[tuple[str, str]], *, execute: bool) -> list[str]:
        if not execute:
            return [path for path, _ in files]
        written: list[str] = []
        for path, content in files:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
            written.append(path)
        return written

    def missed_run_hint(self) -> str:
        return (
            "launchd does not replay calendar jobs that were missed while "
            "the machine was asleep or off"
        )


def _uid() -> int:
    try:
        import os

        return os.getuid()
    except AttributeError:
        return 501
