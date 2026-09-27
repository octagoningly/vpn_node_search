from __future__ import annotations

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


class SystemdError(NodeBenchError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(stage="scheduler", code=code, message=message)


def unit_stem(task: ScheduledTask) -> str:
    return f"{task.name}"


def timer_path(task: ScheduledTask) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{unit_stem(task)}.timer"


def service_path(task: ScheduledTask) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{unit_stem(task)}.service"


def render_timer(task: ScheduledTask) -> str:
    hour, minute = task.time.split(":")
    return (
        "[Unit]\n"
        f"Description=nodebench daily run for profile {task.profile}\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {hour}:{minute}:00\n"
        "Persistent=true\n"
        f"Unit={unit_stem(task)}.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def render_service(task: ScheduledTask) -> str:
    executable = sys.executable or "python"
    return (
        "[Unit]\n"
        f"Description=nodebench run --profile {task.profile}\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={task.root}\n"
        f'ExecStart="{executable}" -m nodebench.cli.main run --profile {task.profile}\n'
        f"StandardOutput=append:{task.log_path}\n"
        f"StandardError=append:{task.log_path}\n"
    )


def render_units(task: ScheduledTask) -> list[tuple[str, str]]:
    return [
        (str(timer_path(task)), render_timer(task)),
        (str(service_path(task)), render_service(task)),
    ]


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
        raise SystemdError(
            code="systemd_unavailable",
            message=f"cannot execute {argv[0]!r}: {exc}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SystemdError(
            code="systemd_timeout",
            message=f"{argv[0]} timed out: {' '.join(argv)}",
        ) from exc
    return CommandResult(
        argv=argv,
        executed=True,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def parse_timer_status(text: str) -> list[TaskStatus]:
    statuses: list[TaskStatus] = []
    for line in text.splitlines():
        token = line.strip()
        if not token:
            continue
        if token.startswith(f"{TASK_PREFIX}-") and token.endswith(".timer"):
            statuses.append(TaskStatus(exists=True, name=token))
    return statuses


class SystemdBackend(SchedulerBackend):
    platform = "linux"

    def __init__(self, runner=None) -> None:
        self._runner = runner or run_command

    def plan(self, task: ScheduledTask, action: str = "install") -> list[list[str]]:
        ensure_owned_name(task.name)
        if action == "uninstall":
            return [
                ["systemctl", "--user", "disable", "--now", f"{unit_stem(task)}.timer"],
            ]
        if action == "status":
            return self.query_plan(task.name)
        return [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", f"{unit_stem(task)}.timer"],
        ]

    def query_plan(self, name: str | None = None) -> list[list[str]]:
        if name:
            ensure_owned_name(name)
            return [
                ["systemctl", "--user", "is-enabled", f"{name}.timer"],
                ["systemctl", "--user", "list-timers", f"{name}.timer"],
            ]
        return [["systemctl", "--user", "list-timers"]]

    def run(self, commands: list[list[str]], *, execute: bool) -> list[CommandResult]:
        return [self._runner(command, execute=execute) for command in commands]

    def parse_query(self, text: str) -> list[TaskStatus]:
        return parse_timer_status(text)

    def render(self, task: ScheduledTask) -> list[tuple[str, str]]:
        return render_units(task)

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
            "systemd timers marked Persistent=true replay missed runs once the "
            "machine is back; check the unit if runs are skipped"
        )
