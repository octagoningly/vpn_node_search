from __future__ import annotations

import argparse
import sys

from nodebench.cli.common import (
    SCHEDULER_ACTIONS,
    UNINSTALL_YES_HINT,
    YES_HINT,
    _confirm_or_hint,
    _print_scheduler_json,
    _print_scheduler_messages,
    _print_scheduler_plan,
    _scheduler_settings,
)
from nodebench.core.errors import ConfigError
from nodebench.scheduler import service as scheduler_service


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
