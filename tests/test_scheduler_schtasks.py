from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from nodebench.core.errors import ConfigError, NodeBenchError, exit_code_for
from nodebench.scheduler.base import (
    MISSED_RUN_HINT,
    CommandResult,
    ScheduledTask,
    build_task,
)
from nodebench.scheduler.schtasks import (
    MAX_TR_CHARS,
    SchtasksBackend,
    SchtasksError,
    build_schtasks_argv,
    clean_name,
    decode_output,
    first_failure,
    guard_argv,
    parse_query,
    parse_query_csv,
    parse_query_list,
    render_plan,
    run_command,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_task(log_path: str | None = None) -> ScheduledTask:
    task = build_task("local", "04:37", root=PROJECT_ROOT, platform="windows")
    if log_path is not None:
        task.log_path = log_path
    return task


ENGLISH_CSV = (
    '"HostName","TaskName","Next Run Time","Status","Last Run Time","Last Result"\r\n'
    '"HOST","\\Other Tool","2026/09/27 04:37:00","Ready","2026/09/26 04:37:00","0"\r\n'
    '"HOST","\\nodebench-local-0437","2026/09/27 04:37:00","Ready","2026/09/26 04:37:00","0"\r\n'
    '"HOST","\\adobe-updater","N/A","Ready","N/A","0"\r\n'
)

CHINESE_CSV = (
    '"主机名","任务名","下次运行时间","状态","上次运行时间","上次结果"\r\n'
    '"HOST","\\Other Tool","2026/09/27 04:37:00","就绪","2026/09/26 04:37:00","0"\r\n'
    '"HOST","\\nodebench-github-0500","2026/09/27 05:00:00","就绪","2026/09/26 05:00:00","0"\r\n'
)

MOJIBAKE_CSV = (
    '"\ufffd\ufffd\ufffd\ufffd","TaskX","NextX"\r\n'
    '"HOST","\\nodebench-local-0437","2026/09/27 04:37:00"\r\n'
)

ENGLISH_LIST = (
    "TaskName:       \\nodebench-local-0437\r\n"
    "Next Run Time:  2026/09/27 04:37:00\r\n"
    "Status:         Ready\r\n"
    "Last Result:    0\r\n"
)


def test_wrap_command_redirects_into_project_log():
    task = make_task()
    argv = build_schtasks_argv(task)
    assert argv[0] == "schtasks"
    assert argv[1] == "/Create"
    assert argv[argv.index("/TN") + 1] == "nodebench-local-0437"
    wrapped = argv[argv.index("/TR") + 1]
    assert wrapped.startswith('cmd.exe /c "')
    assert wrapped.endswith('"')
    assert "cd /d" in wrapped
    assert task.root in wrapped
    assert "-m nodebench.cli.main run" in wrapped
    assert f'--profile "local"' in wrapped
    assert f'>> "{task.log_path}" 2>&1' in wrapped
    assert argv[argv.index("/SC") + 1] == "DAILY"
    assert argv[argv.index("/ST") + 1] == "04:37"
    assert argv[-1] == "/F"


def test_build_schtasks_argv_refuses_oversized_commands():
    task = make_task(log_path="C:\\" + ("x" * 400) + ".log")
    with pytest.raises(ConfigError) as exc:
        build_schtasks_argv(task)
    assert exc.value.code == "scheduler_command_too_long"
    assert exit_code_for(exc.value) == 2
    assert str(MAX_TR_CHARS) in exc.value.message


def test_guard_allows_project_tasks_and_read_only_queries():
    guard_argv(["schtasks", "/Delete", "/TN", "nodebench-local-0437", "/F"])
    guard_argv(["schtasks", "/Query", "/FO", "CSV", "/V"])
    guard_argv(["schtasks", "/Query", "/TN", "nodebench-local-0437", "/FO", "LIST", "/V"])


def test_guard_refuses_foreign_task_names():
    with pytest.raises(ConfigError) as exc:
        guard_argv(["schtasks", "/Delete", "/TN", "adobe-updater", "/F"])
    assert exc.value.code == "scheduler_safety_gate"
    assert "nodebench-*" in exc.value.message
    assert exit_code_for(exc.value) == 2


def test_guard_refuses_mutating_switch_without_target():
    with pytest.raises(ConfigError) as exc:
        guard_argv(["schtasks", "/Create", "/TR", "whatever"])
    assert exc.value.code == "scheduler_safety_gate"
    with pytest.raises(ConfigError) as exc:
        guard_argv(["schtasks", "/change", "/F"])
    assert exc.value.code == "scheduler_safety_gate"


def test_guard_runs_before_execute_flag():
    with pytest.raises(ConfigError) as exc:
        run_command(["schtasks", "/Delete", "/TN", "foreign", "/F"], execute=False)
    assert exc.value.code == "scheduler_safety_gate"


def test_run_command_skips_subprocess_when_not_executing(monkeypatch):
    def refuse(*args, **kwargs):  # pragma: no cover - guard
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr(subprocess, "run", refuse)
    result = run_command(["schtasks", "/Query", "/FO", "CSV", "/V"], execute=False)
    assert result.executed is False
    assert result.output == ""


def test_run_command_decodes_stdout(monkeypatch):
    def fake_run(argv, **kwargs):
        assert kwargs["capture_output"] is True
        return SimpleNamespace(
            returncode=0,
            stdout='"主机名","任务名"\n'.encode("gbk"),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_command(["schtasks", "/Query", "/FO", "CSV", "/V"], execute=True)
    assert result.executed is True
    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == '"主机名","任务名"'
    assert result.ok is True


def test_run_command_reports_missing_binary(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("no schtasks")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SchtasksError) as exc:
        run_command(["schtasks", "/Query"], execute=True)
    assert exc.value.code == "schtasks_unavailable"
    assert isinstance(exc.value, NodeBenchError)
    assert exc.value.stage == "scheduler"
    assert exit_code_for(exc.value) == 4


def test_run_command_reports_timeout(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=60)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SchtasksError) as exc:
        run_command(["schtasks", "/Query"], execute=True)
    assert exc.value.code == "schtasks_timeout"
    assert exit_code_for(exc.value) == 4


def test_decode_output_prefers_utf8_then_console_codepage():
    assert decode_output("ok".encode("utf-8")) == "ok"
    assert decode_output("主机名".encode("gbk")) == "主机名"
    assert decode_output(None) == ""
    assert decode_output("already text") == "already text"


def test_clean_name_strips_task_path_separator():
    assert clean_name("\\nodebench-local-0437") == "nodebench-local-0437"
    assert clean_name("  \\nodebench-local-0437  ") == "nodebench-local-0437"
    assert clean_name("plain") == "plain"


def test_parse_query_csv_english_headers():
    statuses = parse_query(ENGLISH_CSV)
    assert [status.name for status in statuses] == ["nodebench-local-0437"]
    status = statuses[0]
    assert status.exists is True
    assert status.next_run == "2026/09/27 04:37:00"
    assert status.last_result == "0"


def test_parse_query_csv_chinese_headers():
    statuses = parse_query(CHINESE_CSV)
    assert [status.name for status in statuses] == ["nodebench-github-0500"]
    assert statuses[0].next_run == "2026/09/27 05:00:00"


def test_parse_query_csv_without_recognizable_headers():
    statuses = parse_query(MOJIBAKE_CSV)
    assert [status.name for status in statuses] == ["nodebench-local-0437"]


def test_parse_query_csv_ignores_rows_without_project_tasks():
    text = (
        '"HostName","TaskName"\r\n'
        '"HOST","\\Other Tool"\r\n'
        '"HOST","\\adobe-updater"\r\n'
    )
    assert parse_query(text) == []


def test_parse_query_list_english_keys():
    statuses = parse_query(ENGLISH_LIST)
    assert [status.name for status in statuses] == ["nodebench-local-0437"]
    assert statuses[0].next_run == "2026/09/27 04:37:00"
    assert statuses[0].last_result == "0"


def test_parse_query_list_chinese_keys():
    text = (
        "任务名:       \\nodebench-local-0437\r\n"
        "下次运行时间:  2026/09/27 04:37:00\r\n"
        "状态:         就绪\r\n"
    )
    statuses = parse_query(text)
    assert [status.name for status in statuses] == ["nodebench-local-0437"]
    assert statuses[0].next_run == "2026/09/27 04:37:00"


def test_parse_query_list_falls_back_to_token_scan():
    text = "unexpected output\nnodebench-local-0437 Ready\n"
    statuses = parse_query(text)
    assert [status.name for status in statuses] == ["nodebench-local-0437"]


def test_parse_query_dispatches_on_csv_shape():
    assert parse_query_csv(ENGLISH_LIST) == parse_query(ENGLISH_LIST)
    assert len(parse_query(ENGLISH_CSV)) == 1


def test_first_failure_formats_command_output():
    failing = CommandResult(
        argv=["schtasks", "/Create"],
        executed=True,
        returncode=1,
        stderr="denied",
    )
    message = first_failure([failing])
    assert message is not None
    assert "command failed (1)" in message
    assert "denied" in message
    ok = CommandResult(argv=["schtasks", "/Query"], executed=True, returncode=0)
    assert first_failure([ok]) is None
    skipped = CommandResult(argv=["schtasks", "/Query"], executed=False, returncode=1)
    assert first_failure([skipped]) is None


def test_render_plan_quotes_tokens_with_spaces():
    rendered = render_plan(["schtasks", "/TR", "cmd.exe /c x"])
    assert rendered == 'schtasks /TR "cmd.exe /c x"'
    assert render_plan(["schtasks", "/FO", "CSV"]) == "schtasks /FO CSV"


def test_backend_plans_install_uninstall_and_status():
    backend = SchtasksBackend()
    task = make_task()
    install = backend.plan(task, "install")
    assert install[0][1] == "/Create"
    uninstall = backend.plan(task, "uninstall")
    assert uninstall == [["schtasks", "/Delete", "/TN", "nodebench-local-0437", "/F"]]
    assert backend.query_plan(None) == [["schtasks", "/Query", "/FO", "CSV", "/V"]]
    assert backend.query_plan("nodebench-local-0437") == [
        ["schtasks", "/Query", "/TN", "nodebench-local-0437", "/FO", "LIST", "/V"]
    ]
    assert backend.missed_run_hint() == MISSED_RUN_HINT


def test_backend_rejects_foreign_task_names():
    backend = SchtasksBackend()
    task = make_task()
    task.name = "foreign"
    with pytest.raises(ConfigError) as exc:
        backend.plan(task, "install")
    assert exc.value.code == "scheduler_foreign_task"


def test_backend_runs_injected_runner():
    seen: list[list[str]] = []

    def runner(argv, *, execute):
        seen.append(list(argv))
        return CommandResult(argv=list(argv), executed=execute, returncode=0)

    backend = SchtasksBackend(runner=runner)
    backend.run([["schtasks", "/Query", "/FO", "CSV", "/V"]], execute=False)
    backend.run([["schtasks", "/Query", "/FO", "CSV", "/V"]], execute=True)
    assert len(seen) == 2


def test_diagnose_explains_missing_and_denied_tasks():
    backend = SchtasksBackend()
    missing = CommandResult(
        argv=["schtasks", "/Query"],
        executed=True,
        returncode=1,
        stderr="ERROR: The system cannot find the file specified.",
    )
    assert str(backend.diagnose(missing)).startswith("no scheduled task")
    denied = CommandResult(
        argv=["schtasks", "/Delete"],
        executed=True,
        returncode=1,
        stderr="ERROR: Access is denied.",
    )
    assert "access denied" in str(backend.diagnose(denied))
    healthy = CommandResult(argv=["schtasks"], executed=True, returncode=0)
    assert backend.diagnose(healthy) is None
