from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from nodebench.core.errors import ConfigError

TASK_PREFIX = "nodebench"
MAX_TASK_NAME = 64
MAX_TR_CHARS = 261
TIME_PATTERN = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
TASK_NAME_TOKEN = re.compile(rf"{TASK_PREFIX}-[A-Za-z0-9._-]+")
MISSED_RUN_HINT = (
    "missed runs while the machine is off never replay; enable the missed-run "
    "policy of the Windows Task Scheduler (关机错过的运行不会自动补跑，"
    "请在 Windows 任务计划程序属性中勾选『如果错过计划任务则尽快运行』)"
)


def normalize_platform(value: str | None = None) -> str:
    text = str(value or sys.platform).lower()
    if text.startswith("win"):
        return "windows"
    if text == "darwin" or text.startswith("mac"):
        return "macos"
    if text.startswith("linux"):
        return "linux"
    return text


def find_project_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "config" / "default.yaml").is_file():
            return candidate
    cwd = Path.cwd()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "config" / "default.yaml").is_file():
            return candidate
    return here.parents[3]


def sanitize_time(value: object) -> str:
    text = str(value if value is not None else "").strip()
    if not TIME_PATTERN.match(text):
        raise ConfigError(
            code="scheduler_time_invalid",
            message=(
                f"invalid --time {text!r}: expected HH:MM between 00:00 and 23:59"
            ),
        )
    return text


def sanitize_name_part(value: object) -> str:
    text = str(value if value is not None else "").strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-._")


def build_task_name(profile: object, time_value: object) -> str:
    profile_part = sanitize_name_part(profile) or "default"
    time_part = sanitize_name_part(sanitize_time(time_value).replace(":", ""))
    name = f"{TASK_PREFIX}-{profile_part}-{time_part}"
    if len(name) > MAX_TASK_NAME:
        allowed = MAX_TASK_NAME - len(f"{TASK_PREFIX}-{time_part}") - 1
        if allowed < 1:
            raise ConfigError(
                code="scheduler_name_invalid",
                message=f"scheduled task name too long: {name}",
            )
        profile_part = profile_part[:allowed].rstrip("-._") or "default"
        name = f"{TASK_PREFIX}-{profile_part}-{time_part}"
    return name.lower()


def ensure_owned_name(name: object) -> str:
    text = str(name if name is not None else "").strip()
    if not text.startswith(f"{TASK_PREFIX}-"):
        raise ConfigError(
            code="scheduler_foreign_task",
            message=(
                f"refusing to touch scheduled task {text!r}: only tasks named "
                f"{TASK_PREFIX}-* belong to this project"
            ),
        )
    if not NAME_PATTERN.match(text):
        raise ConfigError(
            code="scheduler_name_invalid",
            message=f"invalid scheduled task name {text!r}",
        )
    return text


def profile_from_task_name(name: object) -> str | None:
    text = str(name if name is not None else "").strip()
    if not text.startswith(f"{TASK_PREFIX}-"):
        return None
    rest = text[len(TASK_PREFIX) + 1 :]
    if not rest:
        return None
    return rest.rsplit("-", 1)[0] or None


def profile_prefix(profile: object) -> str:
    return f"{TASK_PREFIX}-{sanitize_name_part(profile)}-"


@dataclass
class ScheduledTask:
    name: str
    profile: str
    time: str
    command: str
    platform: str
    log_path: str = ""
    root: str = ""
    argv: list[str] = field(default_factory=list)

    @property
    def display(self) -> str:
        return (
            f"{self.name} | profile={self.profile} | time={self.time} | "
            f"platform={self.platform}"
        )


def build_command(profile: str) -> str:
    executable = sys.executable or "python"
    return f'"{executable}" -m nodebench.cli.main run --profile "{profile}"'


def build_task(
    profile: object,
    time_value: object,
    *,
    root: Path | str | None = None,
    output_dir: str = "output",
    platform: str | None = None,
) -> ScheduledTask:
    profile_text = str(profile if profile is not None else "").strip()
    if not profile_text:
        raise ConfigError(
            code="scheduler_profile_required",
            message="a profile is required to build a scheduled task",
        )
    clean_time = sanitize_time(time_value)
    resolved_root = Path(root) if root is not None else find_project_root()
    name = build_task_name(profile_text, clean_time)
    log_path = Path(output_dir or "output") / "logs" / f"scheduler-{name}.log"
    if not log_path.is_absolute():
        log_path = resolved_root / log_path
    return ScheduledTask(
        name=name,
        profile=profile_text,
        time=clean_time,
        command=build_command(profile_text),
        platform=normalize_platform(platform),
        log_path=str(log_path),
        root=str(resolved_root),
        argv=[sys.executable, "-m", "nodebench.cli.main", "run", "--profile", profile_text],
    )


def validate_task(task: ScheduledTask, root: Path | str | None = None) -> ScheduledTask:
    ensure_owned_name(task.name)
    if not NAME_PATTERN.match(task.name):
        raise ConfigError(
            code="scheduler_name_invalid",
            message=f"invalid scheduled task name {task.name!r}",
        )
    base = Path(root) if root is not None else find_project_root()
    profile_file = base / "config" / "profiles" / f"{task.profile}.yaml"
    if not profile_file.is_file():
        raise ConfigError(
            code="scheduler_profile_unknown",
            message=(
                f"unknown profile {task.profile!r}: expected profile file "
                f"{profile_file}"
            ),
        )
    return task


def build_scheduled_task(
    profile: object,
    time_value: object,
    *,
    root: Path | str | None = None,
    output_dir: str = "output",
    platform: str | None = None,
) -> ScheduledTask:
    task = build_task(
        profile,
        time_value,
        root=root,
        output_dir=output_dir,
        platform=platform,
    )
    return validate_task(task, root=root)


@dataclass
class CommandResult:
    argv: list[str]
    executed: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""

    @property
    def output(self) -> str:
        return f"{self.stdout}{self.stderr}"

    @property
    def ok(self) -> bool:
        return self.executed and self.returncode == 0


@dataclass
class TaskStatus:
    exists: bool = False
    name: str = ""
    next_run: str | None = None
    last_run: str | None = None
    last_result: str | None = None
    raw_excerpt: str = ""


class SchedulerBackend:
    platform = "unknown"

    def plan(
        self, task: ScheduledTask, action: str = "install"
    ) -> list[list[str]]:
        raise NotImplementedError

    def query_plan(self, name: str | None = None) -> list[list[str]]:
        raise NotImplementedError

    def run(
        self, commands: list[list[str]], *, execute: bool
    ) -> list[CommandResult]:
        raise NotImplementedError

    def parse_query(self, text: str) -> list[TaskStatus]:
        raise NotImplementedError

    def render(self, task: ScheduledTask) -> list[tuple[str, str]]:
        return []

    def write(
        self, files: list[tuple[str, str]], *, execute: bool
    ) -> list[str]:
        return []

    def missed_run_hint(self) -> str:
        return ""


__all__ = [
    "TASK_PREFIX",
    "MAX_TASK_NAME",
    "MISSED_RUN_HINT",
    "ScheduledTask",
    "CommandResult",
    "TaskStatus",
    "SchedulerBackend",
    "normalize_platform",
    "find_project_root",
    "sanitize_time",
    "sanitize_name_part",
    "build_task_name",
    "ensure_owned_name",
    "profile_from_task_name",
    "profile_prefix",
    "build_command",
    "build_task",
    "validate_task",
    "build_scheduled_task",
]
