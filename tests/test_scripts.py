from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"

SHELL_SCRIPTS = ["run.sh", "install.sh"]
POWERSHELL_SCRIPTS = ["run.ps1", "install.ps1"]
CMD_SCRIPTS = ["run.bat"]
RUN_SCRIPTS = SHELL_SCRIPTS[:1] + POWERSHELL_SCRIPTS[:1] + CMD_SCRIPTS
INSTALL_SCRIPTS = SHELL_SCRIPTS[1:] + POWERSHELL_SCRIPTS[1:]
UTF8_SCRIPTS = SHELL_SCRIPTS + ["README.md"]
ASCII_SCRIPTS = POWERSHELL_SCRIPTS + CMD_SCRIPTS


def read_bytes(name: str) -> bytes:
    return (SCRIPTS / name).read_bytes()


def read_text(name: str) -> str:
    return read_bytes(name).decode("utf-8")


@pytest.mark.parametrize("name", SHELL_SCRIPTS + POWERSHELL_SCRIPTS + CMD_SCRIPTS + ["README.md"])
def test_scripts_exist(name):
    path = SCRIPTS / name
    assert path.is_file()
    assert path.stat().st_size > 0


@pytest.mark.parametrize("name", UTF8_SCRIPTS)
def test_text_files_are_utf8_without_bom(name):
    data = read_bytes(name)
    assert not data.startswith(b"\xef\xbb\xbf")
    data.decode("utf-8")


@pytest.mark.parametrize("name", ASCII_SCRIPTS)
def test_windows_scripts_stay_ascii(name):
    data = read_bytes(name)
    assert not data.startswith(b"\xef\xbb\xbf")
    data.decode("ascii")


@pytest.mark.parametrize("name", SHELL_SCRIPTS)
def test_shell_scripts_use_lf_endings(name):
    data = read_bytes(name)
    assert b"\r" not in data
    assert data.startswith(b"#!/usr/bin/env bash\n")
    assert b"set -euo pipefail" in data


@pytest.mark.parametrize("name", POWERSHELL_SCRIPTS + CMD_SCRIPTS)
def test_windows_scripts_use_crlf_endings(name):
    data = read_bytes(name)
    assert b"\r\n" in data
    assert data.count(b"\n") == data.count(b"\r\n")


def test_run_scripts_call_the_shared_cli():
    assert 'uv run nodebench run --profile "$PROFILE" "$@"' in read_text("run.sh")
    assert "uv run nodebench run --profile $profileName @args" in read_text("run.ps1")
    assert "uv run nodebench run --profile %PROFILE% %*" in read_text("run.bat")


@pytest.mark.parametrize("name", RUN_SCRIPTS)
def test_run_scripts_keep_the_profile_override(name):
    text = read_text(name)
    assert "NODEBENCH_PROFILE" in text
    assert "local" in text


@pytest.mark.parametrize("name", RUN_SCRIPTS)
def test_run_scripts_write_local_logs(name):
    text = read_text(name)
    assert "logs" in text
    assert "run-" in text
    assert "output" in text.lower()


@pytest.mark.parametrize("name", RUN_SCRIPTS)
def test_run_scripts_propagate_exit_codes(name):
    text = read_text(name)
    assert "exit" in text
    assert "uv" in text


@pytest.mark.parametrize("name", SHELL_SCRIPTS + POWERSHELL_SCRIPTS + CMD_SCRIPTS)
def test_all_scripts_report_a_missing_uv(name):
    text = read_text(name)
    assert "uv not found on PATH" in text
    assert "never downloads tools" in text


@pytest.mark.parametrize("name", SHELL_SCRIPTS + POWERSHELL_SCRIPTS + CMD_SCRIPTS)
def test_all_scripts_never_download_anything(name):
    text = read_text(name).lower()
    for token in ("curl ", "wget ", "invoke-webrequest", "iwr ", "irm ", "start-bitstransfer"):
        assert token not in text


@pytest.mark.parametrize("name", INSTALL_SCRIPTS)
def test_install_scripts_sync_and_diagnose(name):
    text = read_text(name)
    assert "uv sync" in text
    assert "uv run nodebench doctor" in text
    assert "3.12" in text
    assert "never downloads tools" in text


def test_install_scripts_require_uv_before_any_step():
    for name in INSTALL_SCRIPTS:
        text = read_text(name)
        assert text.index("uv not found on PATH") < text.index("uv sync")


def test_install_sh_gates_on_minor_version():
    text = read_text("install.sh")
    assert "if (( minor >= 12 ))" in text
    assert "^Python\\ 3\\.([0-9]+)" in text
    assert "major > 3" not in text


def test_run_sh_captures_and_reprints_the_log():
    text = read_text("run.sh")
    assert 'LOG="$ROOT/output/logs/run-${STAMP}.log"' in text
    assert 'uv run nodebench run --profile "$PROFILE" "$@" >"$LOG" 2>&1' in text
    assert 'cat "$LOG"' in text
    assert 'exit "$CODE"' in text


def test_run_bat_captures_and_reprints_the_log():
    text = read_text("run.bat")
    assert 'set "LOG=output\\logs\\run-%STAMP%.log"' in text
    assert 'uv run nodebench run --profile %PROFILE% %* > "%LOG%" 2>&1' in text
    assert 'type "%LOG%"' in text
    assert "exit /b %CODE%" in text


def test_run_ps1_captures_and_reprints_the_log():
    text = read_text("run.ps1")
    assert '"run-$stamp.log"' in text
    assert "Tee-Object" in text
    assert "exit $code" in text


def test_readme_documents_usage_and_endings():
    text = read_text("README.md")
    assert "chmod +x scripts/run.sh scripts/install.sh" in text
    assert "scheduler install --profile local --time 04:37 --yes" in text
    assert "scheduler status" in text
    assert "scheduler uninstall" in text
    assert "LF line endings" in text
    assert "CRLF" in text
