from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nodebench.core.errors import ConfigError, exit_code_for
from nodebench.scheduler.base import (
    MAX_TASK_NAME,
    build_task,
    build_scheduled_task,
    build_task_name,
    ensure_owned_name,
    normalize_platform,
    profile_from_task_name,
    profile_prefix,
    sanitize_name_part,
    sanitize_time,
    validate_task,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("value", ["04:37", "00:00", "23:59", " 09:05 "])
def test_sanitize_time_accepts_valid_times(value):
    assert sanitize_time(value) == value.strip()


@pytest.mark.parametrize("value", ["24:00", "25:00", "4:37", "0437", "04:60", "abc", "", None])
def test_sanitize_time_rejects_invalid_times(value):
    with pytest.raises(ConfigError) as exc:
        sanitize_time(value)
    assert exc.value.code == "scheduler_time_invalid"
    assert exc.value.stage == "config"
    assert exit_code_for(exc.value) == 2
    assert "HH:MM" in exc.value.message


def test_sanitize_name_part_collapses_separators():
    assert sanitize_name_part("my profile") == "my-profile"
    assert sanitize_name_part("a--b") == "a-b"
    assert sanitize_name_part(" ..name.. ") == "name"
    assert sanitize_name_part("!!!") == ""


def test_build_task_name_keeps_time_and_profile_parts():
    assert build_task_name("local", "04:37") == "nodebench-local-0437"
    assert build_task_name("LOCAL", "04:37") == "nodebench-local-0437"
    assert build_task_name("my profile", "09:05") == "nodebench-my-profile-0905"
    assert build_task_name("", "04:37") == "nodebench-default-0437"


def test_build_task_name_never_contains_colon():
    assert ":" not in build_task_name("local", "04:37")


def test_build_task_name_truncates_long_profiles():
    name = build_task_name("p" * 120, "04:37")
    assert len(name) <= MAX_TASK_NAME
    assert name.startswith("nodebench-")
    assert name.endswith("-0437")
    assert name.islower()


def test_ensure_owned_name_accepts_project_tasks():
    assert ensure_owned_name("nodebench-local-0437") == "nodebench-local-0437"
    assert ensure_owned_name("  nodebench-github-0500 ") == "nodebench-github-0500"


def test_ensure_owned_name_refuses_foreign_tasks():
    with pytest.raises(ConfigError) as exc:
        ensure_owned_name("adobe-updater")
    assert exc.value.code == "scheduler_foreign_task"
    assert exit_code_for(exc.value) == 2
    assert "nodebench-*" in exc.value.message


def test_ensure_owned_name_refuses_illegal_characters():
    with pytest.raises(ConfigError) as exc:
        ensure_owned_name("nodebench-local-04 37")
    assert exc.value.code == "scheduler_name_invalid"
    assert exit_code_for(exc.value) == 2


def test_profile_from_task_name():
    assert profile_from_task_name("nodebench-local-0437") == "local"
    assert profile_from_task_name("nodebench-my-profile-0437") == "my-profile"
    assert profile_from_task_name("adobe-updater") is None
    assert profile_from_task_name("nodebench-") is None


def test_profile_prefix():
    assert profile_prefix("local") == "nodebench-local-"
    assert profile_prefix("my profile") == "nodebench-my-profile-"


def test_normalize_platform_maps_known_platforms():
    assert normalize_platform("win32") == "windows"
    assert normalize_platform("Windows") == "windows"
    assert normalize_platform("darwin") == "macos"
    assert normalize_platform("macos") == "macos"
    assert normalize_platform("linux") == "linux"


def test_normalize_platform_defaults_to_current_machine():
    expected = "windows" if sys.platform.startswith("win") else normalize_platform(sys.platform)
    assert normalize_platform(None) == expected


def test_build_task_requires_profile():
    with pytest.raises(ConfigError) as exc:
        build_task("", "04:37", root=PROJECT_ROOT, platform="windows")
    assert exc.value.code == "scheduler_profile_required"
    assert exit_code_for(exc.value) == 2


def test_build_task_shapes_command_and_log_path(tmp_path: Path):
    task = build_task(
        "local",
        "04:37",
        root=tmp_path,
        output_dir="out",
        platform="windows",
    )
    assert task.name == "nodebench-local-0437"
    assert task.profile == "local"
    assert task.time == "04:37"
    assert task.platform == "windows"
    assert task.root == str(tmp_path)
    assert '-m nodebench.cli.main run --profile "local"' in task.command
    assert task.command.startswith(f'"{sys.executable}"')
    assert task.argv == [sys.executable, "-m", "nodebench.cli.main", "run", "--profile", "local"]
    assert task.log_path == str(tmp_path / "out" / "logs" / "scheduler-nodebench-local-0437.log")
    assert "profile=local" in task.display
    assert "time=04:37" in task.display


def test_build_task_rejects_bad_time(tmp_path: Path):
    with pytest.raises(ConfigError) as exc:
        build_task("local", "25:00", root=tmp_path, platform="windows")
    assert exc.value.code == "scheduler_time_invalid"


def test_validate_task_requires_existing_profile_file(tmp_path: Path):
    task = build_task("local", "04:37", root=tmp_path, platform="windows")
    with pytest.raises(ConfigError) as exc:
        validate_task(task, root=tmp_path)
    assert exc.value.code == "scheduler_profile_unknown"
    assert exit_code_for(exc.value) == 2
    profile_file = tmp_path / "config" / "profiles" / "local.yaml"
    profile_file.parent.mkdir(parents=True, exist_ok=True)
    profile_file.write_text("probe: {}\n", encoding="utf-8")
    assert validate_task(task, root=tmp_path) is task


def test_validate_task_refuses_foreign_names(tmp_path: Path):
    task = build_task("local", "04:37", root=tmp_path, platform="windows")
    task.name = "foreign-task"
    with pytest.raises(ConfigError) as exc:
        validate_task(task, root=tmp_path)
    assert exc.value.code == "scheduler_foreign_task"


def test_build_scheduled_task_validates_profile(tmp_path: Path):
    with pytest.raises(ConfigError) as exc:
        build_scheduled_task("ghost", "04:37", root=tmp_path, platform="windows")
    assert exc.value.code == "scheduler_profile_unknown"


def test_build_scheduled_task_accepts_real_profile(tmp_path: Path):
    profile_file = tmp_path / "config" / "profiles" / "local.yaml"
    profile_file.parent.mkdir(parents=True, exist_ok=True)
    profile_file.write_text("probe: {}\n", encoding="utf-8")
    task = build_scheduled_task("local", "04:37", root=tmp_path, platform="windows")
    assert task.name == "nodebench-local-0437"
