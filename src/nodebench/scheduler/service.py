from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from nodebench.core.errors import ConfigError, NodeBenchError
from nodebench.scheduler.base import (
    TASK_PREFIX,
    SchedulerBackend,
    ScheduledTask,
    build_scheduled_task,
    build_task,
    ensure_owned_name,
    normalize_platform,
    profile_from_task_name,
    profile_prefix,
    sanitize_time,
    validate_task,
)
from nodebench.scheduler.launchd import LaunchdBackend
from nodebench.scheduler.schtasks import SchtasksBackend, first_failure, guard_argv
from nodebench.scheduler.systemd import SystemdBackend

DEFAULT_TIME = "04:37"


class SchedulerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    ok: bool
    platform: str
    name: str = ""
    profile: str = ""
    time: str = ""
    dry_run: bool = False
    already_installed: bool = False
    already_absent: bool = False
    exists: bool | None = None
    commands: list[list[str]] = []
    created_files: list[str] = []
    task: dict | None = None
    status: dict | None = None
    statuses: list[dict] = []
    messages: list[str] = []


def get_backend(platform: str | None = None) -> SchedulerBackend:
    normalized = normalize_platform(platform)
    if normalized == "windows":
        return SchtasksBackend()
    if normalized == "macos":
        return LaunchdBackend()
    if normalized == "linux":
        return SystemdBackend()
    raise ConfigError(
        code="scheduler_platform_unsupported",
        message=(
            f"unsupported platform {platform!r}: expected windows, macos or linux"
        ),
    )


def _task_to_dict(task: ScheduledTask) -> dict:
    return {
        "name": task.name,
        "profile": task.profile,
        "time": task.time,
        "platform": task.platform,
        "command": task.command,
        "log_path": task.log_path,
        "root": task.root,
    }


def _apply_name(task: ScheduledTask, name: object | None) -> ScheduledTask:
    if name is None:
        return task
    wanted = ensure_owned_name(name)
    if wanted == task.name:
        return task
    log_path = Path(task.log_path)
    new_log = log_path.with_name(f"scheduler-{wanted}{log_path.suffix}")
    return ScheduledTask(
        name=wanted,
        profile=task.profile,
        time=task.time,
        command=task.command,
        platform=task.platform,
        log_path=str(new_log),
        root=task.root,
        argv=list(task.argv),
    )


def _build_task(
    *,
    profile: str,
    time_value: str | None,
    name: str | None,
    root: Path | str | None,
    output_dir: str,
    platform: str | None,
) -> ScheduledTask:
    clean_time = sanitize_time(time_value or DEFAULT_TIME)
    task = build_scheduled_task(
        profile,
        clean_time,
        root=root,
        output_dir=output_dir,
        platform=platform,
    )
    return _apply_name(task, name)


def _require_success(results, context: str) -> list[str]:
    failure = first_failure(results)
    if failure:
        raise NodeBenchError(
            stage="scheduler",
            code="scheduler_command_failed",
            message=f"{context} failed: {failure}",
        )
    return [result.output.strip() for result in results if result.executed]


def _query_statuses(
    backend: SchedulerBackend,
    commands: list[list[str]],
    *,
    execute: bool,
) -> list[dict]:
    if not execute:
        return []
    results = backend.run(commands, execute=True)
    outputs: list[str] = []
    failed: list = []
    for result in results:
        if result.executed and result.returncode == 0:
            outputs.append(result.output)
        elif result.executed and result.returncode not in (None, 0):
            failed.append(result)
    if failed and not outputs:
        listing = backend.query_plan(None)
        if listing != commands:
            retry = backend.run(listing, execute=True)
            if retry and all(
                item.executed and item.returncode == 0 for item in retry
            ):
                outputs = [item.output for item in retry]
                failed = []
    if failed and not outputs:
        result = failed[0]
        raise NodeBenchError(
            stage="scheduler",
            code="scheduler_query_failed",
            message=(
                f"query failed ({result.returncode}): "
                f"{' '.join(result.argv)}\n{result.output.strip()}"
            ),
        )
    statuses: list[dict] = []
    for output in outputs:
        for status in backend.parse_query(output):
            statuses.append(status.__dict__)
    seen: set[str] = set()
    unique: list[dict] = []
    for status in statuses:
        key = str(status.get("name") or "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(status)
    return unique


def install(
    *,
    profile: str,
    time_value: str | None = None,
    name: str | None = None,
    root: Path | str | None = None,
    output_dir: str = "output",
    platform: str | None = None,
    dry_run: bool = False,
) -> SchedulerResult:
    task = _build_task(
        profile=profile,
        time_value=time_value,
        name=name,
        root=root,
        output_dir=output_dir,
        platform=platform,
    )
    backend = get_backend(task.platform)
    commands = backend.plan(task, "install")
    for command in commands:
        guard_argv(command)
    messages: list[str] = []
    already_installed = False
    created_files: list[str] = []

    if dry_run:
        return SchedulerResult(
            action="install",
            ok=True,
            platform=task.platform,
            name=task.name,
            profile=task.profile,
            time=task.time,
            dry_run=True,
            commands=commands,
            task=_task_to_dict(task),
            messages=[f"dry run: would install {task.name}"],
        )

    pre_statuses = _query_statuses(
        backend, backend.query_plan(task.name), execute=True
    )
    already_installed = any(
        str(status.get("name") or "").endswith(task.name)
        for status in pre_statuses
    )

    files = backend.render(task)
    if files:
        created_files = backend.write(files, execute=True)

    results = backend.run(commands, execute=True)
    outputs = _require_success(results, f"install {task.name}")

    for output in outputs:
        if output:
            messages.append(output)
    if already_installed:
        messages.append(f"scheduled task {task.name} already existed, overwritten")
    else:
        messages.append(f"scheduled task {task.name} installed")
    hint = backend.missed_run_hint()
    if hint:
        messages.append(hint)

    return SchedulerResult(
        action="install",
        ok=True,
        platform=task.platform,
        name=task.name,
        profile=task.profile,
        time=task.time,
        already_installed=already_installed,
        commands=commands,
        created_files=created_files,
        task=_task_to_dict(task),
        messages=messages,
    )


def status(
    *,
    profile: str | None = None,
    name: str | None = None,
    time_value: str | None = None,
    root: Path | str | None = None,
    output_dir: str = "output",
    platform: str | None = None,
    dry_run: bool = False,
) -> SchedulerResult:
    backend = get_backend(platform)
    owned_name: str | None = None
    task_dict: dict | None = None
    profile_text = profile or ""
    if name:
        owned_name = ensure_owned_name(name)
        profile_text = profile_text or profile_from_task_name(owned_name) or ""
    if profile_text and not name:
        task = _build_task(
            profile=profile_text,
            time_value=time_value,
            name=None,
            root=root,
            output_dir=output_dir,
            platform=platform,
        )
        owned_name = task.name
        task_dict = _task_to_dict(task)

    commands = backend.query_plan(owned_name)
    for command in commands:
        guard_argv(command)

    if dry_run:
        return SchedulerResult(
            action="status",
            ok=True,
            platform=backend.platform,
            name=owned_name or "",
            profile=profile_text,
            dry_run=True,
            commands=commands,
            task=task_dict,
            messages=["dry run: would query the task scheduler"],
        )

    statuses = _query_statuses(backend, commands, execute=True)
    prefix = profile_prefix(profile_text) if profile_text else "nodebench-"
    if owned_name:
        matches = [
            status_item
            for status_item in statuses
            if str(status_item.get("name") or "") == owned_name
        ]
    else:
        matches = [
            status_item
            for status_item in statuses
            if str(status_item.get("name") or "").startswith(prefix)
        ]

    messages: list[str] = []
    exists = bool(matches)
    if not exists:
        messages.append("no scheduled task for this project")
    hint = backend.missed_run_hint()
    if exists and hint:
        messages.append(hint)

    return SchedulerResult(
        action="status",
        ok=True,
        platform=backend.platform,
        name=owned_name or "",
        profile=profile_text,
        exists=exists,
        commands=commands,
        task=task_dict,
        statuses=matches,
        status=matches[0] if matches else None,
        messages=messages,
    )


def _task_for_name(
    target: str,
    *,
    profile: str | None,
    root: Path | str | None,
    output_dir: str,
    platform: str | None,
    validate: bool = False,
) -> ScheduledTask:
    derived = profile or profile_from_task_name(target) or "default"
    task = build_task(
        profile=derived,
        time_value=DEFAULT_TIME,
        root=root,
        output_dir=output_dir,
        platform=platform,
    )
    task = _apply_name(task, target)
    if validate:
        return validate_task(task, root=root)
    return task


def _absent_result(
    action: str,
    backend: SchedulerBackend,
    *,
    name: str,
    profile: str,
    messages: list[str],
) -> SchedulerResult:
    return SchedulerResult(
        action=action,
        ok=True,
        platform=backend.platform,
        name=name,
        profile=profile,
        already_absent=True,
        exists=False,
        messages=messages,
    )


def uninstall(
    *,
    profile: str | None = None,
    name: str | None = None,
    time_value: str | None = None,
    display_profile: str | None = None,
    root: Path | str | None = None,
    output_dir: str = "output",
    platform: str | None = None,
    dry_run: bool = False,
) -> SchedulerResult:
    backend = get_backend(platform)
    profile_text = profile or ""
    if name:
        targets = [ensure_owned_name(name)]
        prefix = ""
    else:
        prefix = profile_prefix(profile) if profile else f"{TASK_PREFIX}-"
        targets = []

    if dry_run:
        if targets:
            task = _task_for_name(
                targets[0],
                profile=profile,
                root=root,
                output_dir=output_dir,
                platform=platform,
            )
            commands = backend.plan(task, "uninstall")
            for command in commands:
                guard_argv(command)
            return SchedulerResult(
                action="uninstall",
                ok=True,
                platform=task.platform,
                name=task.name,
                profile=task.profile,
                time=task.time,
                dry_run=True,
                commands=commands,
                task=_task_to_dict(task),
                messages=[f"dry run: would uninstall {task.name}"],
            )
        representative = _build_task(
            profile=display_profile or profile_text or "local",
            time_value=time_value,
            name=None,
            root=root,
            output_dir=output_dir,
            platform=platform,
        )
        return SchedulerResult(
            action="uninstall",
            ok=True,
            platform=representative.platform,
            name=representative.name,
            profile=representative.profile,
            time=representative.time,
            dry_run=True,
            commands=[],
            task=_task_to_dict(representative),
            messages=[
                f"dry run: would uninstall every {prefix}* scheduled task "
                f"of this project"
            ],
        )

    statuses = _query_statuses(backend, backend.query_plan(None), execute=True)
    existing = sorted(
        str(status.get("name") or "") for status in statuses
    )
    if targets:
        matched = [target for target in targets if target in existing]
        missing = [target for target in targets if target not in existing]
        if missing and not matched:
            return _absent_result(
                "uninstall",
                backend,
                name=", ".join(missing),
                profile=profile_text,
                messages=[
                    f"scheduled task {missing[0]} is already absent",
                ],
            )
        targets = matched or targets
    else:
        targets = [item for item in existing if item.startswith(prefix)]
        if not targets:
            return _absent_result(
                "uninstall",
                backend,
                name="",
                profile=profile_text,
                messages=["no scheduled task for this project"],
            )

    tasks = [
        _task_for_name(
            target,
            profile=profile,
            root=root,
            output_dir=output_dir,
            platform=platform,
            validate=bool(profile),
        )
        for target in targets
    ]
    commands: list[list[str]] = []
    for task in tasks:
        commands.extend(backend.plan(task, "uninstall"))
    for command in commands:
        guard_argv(command)

    results = backend.run(commands, execute=True)
    _require_success(results, f"uninstall {targets[0]}")

    removed_files: list[str] = []
    for task in tasks:
        for path, _content in backend.render(task):
            target = Path(path)
            if target.is_file():
                target.unlink()
                removed_files.append(str(target))

    return SchedulerResult(
        action="uninstall",
        ok=True,
        platform=backend.platform,
        name=", ".join(targets),
        profile=profile_text,
        time=tasks[0].time,
        exists=False,
        commands=commands,
        created_files=removed_files,
        task=_task_to_dict(tasks[0]),
        messages=[f"scheduled task {target} removed" for target in targets],
    )
