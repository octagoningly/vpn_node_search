from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodebench.cli.main import main
from nodebench.core.errors import ConfigError, NodeBenchError, exit_code_for
from nodebench.scheduler import schtasks
from nodebench.scheduler.base import MISSED_RUN_HINT, CommandResult
from nodebench.scheduler.service import install, status, uninstall

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def csv_listing(names: list[str], foreign: list[str]) -> str:
    rows = ['"HostName","TaskName","Next Run Time","Status","Last Result"']
    for name in list(foreign) + list(names):
        rows.append(f'"HOST","\\{name}","2026/09/27 04:37:00","Ready","0"')
    return "\r\n".join(rows) + "\r\n"


class FakeSchtasks:
    def __init__(self, names: list[str] | None = None, foreign: list[str] | None = None):
        self.names = sorted(set(names or []))
        self.foreign = list(foreign or [])
        self.calls: list[list[str]] = []
        self.fail_create = False

    def __call__(self, argv, *, execute):
        self.calls.append(list(argv))
        if not execute:
            return CommandResult(argv=list(argv), executed=False)
        action = argv[1] if len(argv) > 1 else ""
        if action == "/Query" and "/TN" in argv:
            name = argv[argv.index("/TN") + 1]
            if name in self.names:
                text = (
                    f"TaskName:       \\{name}\r\n"
                    "Next Run Time:  2026/09/27 04:37:00\r\n"
                    "Status:         Ready\r\n"
                )
                return CommandResult(argv=list(argv), executed=True, returncode=0, stdout=text)
            return CommandResult(
                argv=list(argv),
                executed=True,
                returncode=1,
                stderr="ERROR: cannot find the task",
            )
        if action == "/Query":
            return CommandResult(
                argv=list(argv),
                executed=True,
                returncode=0,
                stdout=csv_listing(self.names, self.foreign),
            )
        if action == "/Create":
            if self.fail_create:
                return CommandResult(
                    argv=list(argv),
                    executed=True,
                    returncode=1,
                    stderr="ERROR: access is denied",
                )
            name = argv[argv.index("/TN") + 1]
            self.names = sorted(set(self.names) | {name})
            return CommandResult(
                argv=list(argv),
                executed=True,
                returncode=0,
                stdout="SUCCESS: the scheduled task has been created",
            )
        if action == "/Delete":
            name = argv[argv.index("/TN") + 1]
            if name not in self.names:
                return CommandResult(
                    argv=list(argv),
                    executed=True,
                    returncode=1,
                    stderr="ERROR: cannot find the task",
                )
            self.names = [item for item in self.names if item != name]
            return CommandResult(argv=list(argv), executed=True, returncode=0, stdout="deleted")
        return CommandResult(
            argv=list(argv),
            executed=True,
            returncode=1,
            stderr="unexpected command",
        )


@pytest.fixture
def fake(monkeypatch):
    runner = FakeSchtasks()
    monkeypatch.setattr(schtasks, "run_command", runner)
    return runner


def test_install_dry_run_never_touches_the_scheduler(fake):
    result = install(profile="local", time_value="04:37", dry_run=True, platform="windows")
    assert result.ok is True
    assert result.dry_run is True
    assert result.name == "nodebench-local-0437"
    assert result.commands[0][1] == "/Create"
    assert result.commands[0][3] == "nodebench-local-0437"
    assert result.created_files == []
    assert result.messages == ["dry run: would install nodebench-local-0437"]
    assert fake.calls == []


def test_install_creates_task_and_reports_the_hint(fake):
    result = install(profile="local", time_value="04:37", platform="windows")
    assert result.ok is True
    assert result.already_installed is False
    assert result.name == "nodebench-local-0437"
    assert result.platform == "windows"
    assert fake.names == ["nodebench-local-0437"]
    assert any("installed" in message for message in result.messages)
    assert MISSED_RUN_HINT in result.messages
    assert any(call[1] == "/Create" for call in fake.calls)


def test_install_is_idempotent_for_the_same_task(fake):
    first = install(profile="local", time_value="04:37", platform="windows")
    second = install(profile="local", time_value="04:37", platform="windows")
    assert first.already_installed is False
    assert second.already_installed is True
    assert any("overwritten" in message for message in second.messages)
    assert fake.names == ["nodebench-local-0437"]


def test_install_rejects_unknown_profiles(fake):
    with pytest.raises(ConfigError) as exc:
        install(profile="ghost", time_value="04:37", platform="windows")
    assert exc.value.code == "scheduler_profile_unknown"
    assert exit_code_for(exc.value) == 2
    assert fake.calls == []


def test_install_rejects_invalid_times(fake):
    with pytest.raises(ConfigError) as exc:
        install(profile="local", time_value="25:00", platform="windows")
    assert exc.value.code == "scheduler_time_invalid"
    assert fake.calls == []


def test_install_failure_maps_to_exit_code_four(fake):
    fake.fail_create = True
    with pytest.raises(NodeBenchError) as exc:
        install(profile="local", time_value="04:37", platform="windows")
    assert exc.value.code == "scheduler_command_failed"
    assert exc.value.stage == "scheduler"
    assert exit_code_for(exc.value) == 4


def test_status_reports_installed_task(fake):
    fake.names = ["nodebench-local-0437"]
    result = status(profile="local", time_value="04:37", platform="windows")
    assert result.ok is True
    assert result.exists is True
    assert result.name == "nodebench-local-0437"
    assert [item["name"] for item in result.statuses] == ["nodebench-local-0437"]
    assert result.status["next_run"] == "2026/09/27 04:37:00"
    assert MISSED_RUN_HINT in result.messages


def test_status_reports_missing_task_without_failing(fake):
    result = status(profile="local", time_value="04:37", platform="windows")
    assert result.ok is True
    assert result.exists is False
    assert result.statuses == []
    assert result.messages == ["no scheduled task for this project"]


def test_status_ignores_foreign_tasks(fake):
    fake.foreign = ["adobe-updater", "other-tool"]
    result = status(platform="windows")
    assert result.exists is False
    assert result.messages == ["no scheduled task for this project"]


def test_status_dry_run_lists_query_without_running_it(fake):
    result = status(profile="local", platform="windows", dry_run=True)
    assert result.dry_run is True
    assert result.commands[0][1] == "/Query"
    assert fake.calls == []


def test_uninstall_dry_run_shows_delete_plan_without_touching_system(fake):
    result = uninstall(platform="windows", dry_run=True)
    assert result.dry_run is True
    assert result.messages[0].startswith("dry run: would uninstall every")
    assert fake.calls == []


def test_uninstall_with_explicit_name_shows_delete_command(fake):
    result = uninstall(name="nodebench-local-0437", platform="windows", dry_run=True)
    assert result.commands == [["schtasks", "/Delete", "/TN", "nodebench-local-0437", "/F"]]
    assert fake.calls == []


def test_uninstall_removes_every_project_task(fake):
    fake.names = ["nodebench-github-0500", "nodebench-local-0437"]
    result = uninstall(platform="windows")
    assert result.ok is True
    assert result.name == "nodebench-github-0500, nodebench-local-0437"
    assert fake.names == []
    assert len(result.messages) == 2
    assert all("removed" in message for message in result.messages)


def test_uninstall_profile_scope_keeps_other_profiles(fake):
    fake.names = ["nodebench-github-0500", "nodebench-local-0437"]
    result = uninstall(profile="local", platform="windows")
    assert result.ok is True
    assert result.name == "nodebench-local-0437"
    assert fake.names == ["nodebench-github-0500"]


def test_uninstall_is_idempotent_when_nothing_is_installed(fake):
    result = uninstall(platform="windows")
    assert result.ok is True
    assert result.already_absent is True
    assert result.exists is False
    assert result.messages == ["no scheduled task for this project"]
    assert all(call[1] == "/Query" for call in fake.calls)


def test_uninstall_absent_name_reports_already_absent(fake):
    result = uninstall(name="nodebench-local-0437", platform="windows")
    assert result.ok is True
    assert result.already_absent is True
    assert "already absent" in result.messages[0]
    assert all(call[1] == "/Query" for call in fake.calls)


def test_uninstall_refuses_foreign_names(fake):
    with pytest.raises(ConfigError) as exc:
        uninstall(name="adobe-updater", platform="windows")
    assert exc.value.code == "scheduler_foreign_task"
    assert exit_code_for(exc.value) == 2
    assert fake.calls == []


def test_scheduler_cli_requires_an_action(capsys):
    code = main(["scheduler"])
    out = capsys.readouterr()
    assert code == 2
    assert "scheduler needs an action" in out.err


def test_scheduler_cli_rejects_invalid_time(capsys):
    code = main(["scheduler", "install", "--profile", "local", "--time", "25:00"])
    out = capsys.readouterr()
    assert code == 2
    assert "scheduler_time_invalid" in out.err
    assert "Traceback" not in out.err


def test_scheduler_cli_install_requires_profile(capsys):
    code = main(["scheduler", "install"])
    out = capsys.readouterr()
    assert code == 2
    assert "install needs --profile" in out.err


def test_scheduler_cli_install_prints_plan_and_waits_for_yes(fake, capsys):
    code = main(["scheduler", "install", "--profile", "local", "--time", "04:37"])
    out = capsys.readouterr()
    assert code == 0
    assert "plan install scheduled task:" in out.out
    assert "nodebench-local-0437" in out.out
    assert '--profile "local"' in out.out
    assert "pass --yes to create" in out.out
    assert fake.calls == []


def test_scheduler_cli_install_with_yes_creates_the_task(fake, capsys):
    code = main(["scheduler", "install", "--profile", "local", "--time", "04:37", "--yes"])
    out = capsys.readouterr()
    assert code == 0
    assert "scheduled task nodebench-local-0437 installed" in out.out
    assert fake.names == ["nodebench-local-0437"]
    assert any(call[1] == "/Create" for call in fake.calls)


def test_scheduler_cli_install_dry_run_json(fake, capsys):
    code = main(
        ["scheduler", "install", "--profile", "local", "--time", "04:37", "--dry-run", "--json"]
    )
    out = capsys.readouterr()
    assert code == 0
    payload = json.loads(out.out)
    assert payload["action"] == "install"
    assert payload["dry_run"] is True
    assert payload["name"] == "nodebench-local-0437"
    assert payload["commands"][0][1] == "/Create"
    assert fake.calls == []


def test_scheduler_cli_status_json(fake, capsys):
    fake.names = ["nodebench-local-0437"]
    code = main(["scheduler", "status", "--json"])
    out = capsys.readouterr()
    assert code == 0
    payload = json.loads(out.out)
    assert payload["action"] == "status"
    assert payload["exists"] is True
    assert payload["statuses"][0]["name"] == "nodebench-local-0437"


def test_scheduler_cli_status_without_tasks(fake, capsys):
    code = main(["scheduler", "status"])
    out = capsys.readouterr()
    assert code == 0
    assert "no scheduled task for this project" in out.out


def test_scheduler_cli_uninstall_waits_for_yes(fake, capsys):
    code = main(["scheduler", "uninstall", "--json"])
    out = capsys.readouterr()
    assert code == 0
    payload = json.loads(out.out)
    assert payload["action"] == "uninstall"
    assert payload["dry_run"] is True
    assert payload["messages"] == ["pass --yes to uninstall"]
    assert fake.calls == []


def test_scheduler_cli_uninstall_with_yes_removes_tasks(fake, capsys):
    fake.names = ["nodebench-local-0437"]
    code = main(["scheduler", "uninstall", "--yes"])
    out = capsys.readouterr()
    assert code == 0
    assert "scheduled task nodebench-local-0437 removed" in out.out
    assert fake.names == []


def test_scheduler_cli_maps_scheduler_failures_to_exit_four(fake, capsys):
    fake.fail_create = True
    code = main(["scheduler", "install", "--profile", "local", "--yes"])
    out = capsys.readouterr()
    assert code == 4
    assert "scheduler_command_failed" in out.err
    assert "Traceback" not in out.err
