from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from nodebench.cli.main import (
    DOCTOR_MESSAGE,
    DOCTOR_MESSAGE_PENDING,
    STUB_MESSAGE,
    main,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = PROJECT_ROOT / "input"

URI_TEXT = (
    "vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.10:443"
    "?type=tcp&security=tls&host=node.example.test#sample-192-0-2-10\n"
    "trojan://sample-password@198.51.100.7:443?security=tls"
    "?peer=edge.example.test#sample-198-51-100-7\n"
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("NODEBENCH_"):
            monkeypatch.delenv(name, raising=False)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def read_report(output_dir: Path, name: str) -> dict:
    matches = sorted(output_dir.glob(f"*/{name}"))
    assert len(matches) == 1, f"expected exactly one {name} in {matches}"
    return json.loads(matches[0].read_text(encoding="utf-8"))


def test_doctor_fails_when_mihomo_is_missing(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setenv(
        "NODEBENCH_PROBE__PROXY__MIHOMO_PATH", str(tmp_path / "absent-mihomo")
    )
    code = main(["doctor"])
    out = capsys.readouterr()
    assert code == 2
    assert "[FAIL] mihomo_binary:" in out.out
    assert "[OK] python:" in out.out
    assert "[OK] config:" in out.out
    assert "[OK] input:" in out.out
    assert "[OK] output:" in out.out
    assert DOCTOR_MESSAGE not in out.out
    assert DOCTOR_MESSAGE_PENDING not in out.out


def test_doctor_passes_when_probe_prerequisites_resolve(
    monkeypatch, tmp_path: Path, capsys
):
    fake_binary = tmp_path / "mihomo.exe"
    fake_binary.write_bytes(b"")
    monkeypatch.setenv("NODEBENCH_PROBE__PROXY__MIHOMO_PATH", str(fake_binary))
    monkeypatch.setenv(
        "NODEBENCH_PROBE__PROXY__SPEEDTEST_URL",
        "https://speed.cloudflare.com/__down",
    )
    monkeypatch.setattr(shutil, "which", lambda name: f"/fake/bin/{name}")
    code = main(["doctor"])
    out = capsys.readouterr()
    assert code == 0
    assert "[FAIL]" not in out.out
    assert "[OK] mihomo_binary:" in out.out
    assert DOCTOR_MESSAGE in out.out
    assert DOCTOR_MESSAGE_PENDING not in out.out


def test_doctor_reports_pending_when_speedtest_url_missing(
    monkeypatch, tmp_path: Path, capsys
):
    fake_binary = tmp_path / "mihomo.exe"
    fake_binary.write_bytes(b"")
    monkeypatch.setenv("NODEBENCH_PROBE__PROXY__MIHOMO_PATH", str(fake_binary))
    monkeypatch.setattr(shutil, "which", lambda name: f"/fake/bin/{name}")
    code = main(["doctor"])
    out = capsys.readouterr()
    assert code == 0
    assert "[WARN] speedtest_url:" in out.out
    assert DOCTOR_MESSAGE_PENDING in out.out
    assert DOCTOR_MESSAGE not in out.out


def test_doctor_reports_missing_cf_target(monkeypatch, capsys):
    monkeypatch.setenv("NODEBENCH_SOURCES__CF__ENABLED", "true")
    monkeypatch.setenv("NODEBENCH_PROBE__CF__TARGET_HOST", "")
    code = main(["doctor"])
    out = capsys.readouterr()
    assert code == 2
    assert "[FAIL] environment:" in out.out
    assert "probe.cf.target_host" in out.out
    assert DOCTOR_MESSAGE not in out.out


def test_doctor_never_prints_secret_values(monkeypatch, capsys):
    monkeypatch.setenv("NODEBENCH_GITHUB_TOKEN", "ghp_example_token_value")
    monkeypatch.setenv("NODEBENCH_PROBE__PROXY__MIHOMO_PATH", "absent-mihomo-binary")
    code = main(["doctor"])
    out = capsys.readouterr()
    assert code == 2
    assert "ghp_example_token_value" not in out.out
    assert "nodebench_github_token: set" in out.out


def test_run_dry_run_writes_report(tmp_path: Path, capsys):
    source = write(tmp_path / "input" / "nodes.txt", URI_TEXT)
    output_dir = tmp_path / "out"
    code = main(
        [
            "run",
            "--dry-run",
            "--input",
            str(source),
            "--output-dir",
            str(output_dir),
        ]
    )
    out = capsys.readouterr()
    assert code == 0
    assert "status=ok" in out.out
    report = read_report(output_dir, "dry-run-report.json")
    assert report["dry_run"] is True
    assert report["status"] == "ok"
    assert report["counts"]["proxy_nodes"] == 2
    assert report["counts"]["edge_endpoints"] == 0
    assert "sample-password" not in json.dumps(report)
    assert not list(output_dir.glob("*/run-report.json"))


def test_run_without_dry_run_writes_run_report(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.setenv(
        "NODEBENCH_PROBE__PROXY__MIHOMO_PATH", str(tmp_path / "absent-mihomo")
    )
    source = write(tmp_path / "input" / "nodes.txt", URI_TEXT)
    output_dir = tmp_path / "out"
    code = main(
        ["run", "--input", str(source), "--output-dir", str(output_dir)]
    )
    capsys.readouterr()
    assert code == 4
    report = read_report(output_dir, "run-report.json")
    assert report["dry_run"] is False
    assert report["status"] == "partial"
    proxy = report["probe"]["proxy"]
    assert proxy["mode"] == "skip"
    assert proxy["skipped_reason"] == "missing_binary"
    assert len(list(output_dir.glob("*/probe-results.json"))) == 1
    assert not list(output_dir.glob("*/dry-run-report.json"))
    payload = json.loads(
        list(output_dir.glob("*/probe-results.json"))[0].read_text(encoding="utf-8")
    )
    assert all(item["probe_mode"] == "not_run" for item in payload["results"])


def test_run_rejects_config_error(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setenv("NODEBENCH_SOURCES__CF__ENABLED", "true")
    monkeypatch.setenv("NODEBENCH_PROBE__CF__TARGET_HOST", "")
    code = main(["run", "--dry-run", "--output-dir", str(tmp_path / "out")])
    out = capsys.readouterr()
    assert code == 2
    assert "error [stage=config, code=cf_target_host_missing]:" in out.err
    assert "probe.cf.target_host" in out.err
    assert out.out == ""


def test_run_fails_when_every_source_fails(tmp_path: Path, capsys):
    output_dir = tmp_path / "out"
    code = main(
        [
            "run",
            "--dry-run",
            "--input",
            str(tmp_path / "absent.txt"),
            "--output-dir",
            str(output_dir),
        ]
    )
    capsys.readouterr()
    assert code == 3
    report = read_report(output_dir, "dry-run-report.json")
    assert report["status"] == "failed"
    assert report["source_reports"][0]["errors"][0]["code"] == "path_missing"


def test_run_unknown_profile_fails(capsys):
    code = main(["run", "--profile", "absent-profile"])
    out = capsys.readouterr()
    assert code == 2
    assert "absent-profile.yaml" in out.err
    assert out.out == ""


def test_collect_prints_source_reports(tmp_path: Path, capsys):
    source = write(tmp_path / "input" / "nodes.txt", URI_TEXT)
    code = main(["collect", "--input", str(source)])
    out = capsys.readouterr()
    assert code == 0
    assert "source local: ok=True fetched=1 errors=0" in out.out
    assert "raw_items=1 proxy_nodes=2 edge_endpoints=0 issues=0 status=ok" in out.out


def test_doctor_resolves_project_root_from_any_cwd(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NODEBENCH_PROBE__PROXY__MIHOMO_PATH", "absent-mihomo-binary")
    code = main(["doctor"])
    out = capsys.readouterr()
    assert code == 2
    assert "[OK] input: 6 sample files" in out.out
    assert "[FAIL] mihomo_binary:" in out.out
    assert DOCTOR_MESSAGE not in out.out


def test_collect_resolves_relative_paths_against_cwd(
    monkeypatch, tmp_path: Path, capsys
):
    monkeypatch.chdir(tmp_path)
    code = main(["collect"])
    out = capsys.readouterr()
    assert code == 3
    assert "path_missing" in out.out
    assert "status=failed" in out.out


def test_project_samples_stay_reachable():
    assert len(list(INPUT_DIR.glob("*"))) == 6


def test_stub_stage_is_not_implemented(capsys):
    code = main(["inspect"])
    out = capsys.readouterr()
    assert code == 0
    assert "Usage: nodebench inspect --run-id" in out.out


def test_unknown_command_returns_two(capsys):
    code = main(["nope"])
    out = capsys.readouterr()
    assert code == 2
    assert "invalid choice" in out.err


def test_help_returns_zero(capsys):
    code = main(["--help"])
    out = capsys.readouterr()
    assert code == 0
    assert "nodebench" in out.out
    run_code = main(["run", "--help"])
    run_out = capsys.readouterr()
    assert run_code == 0
    assert "--dry-run" in run_out.out
    assert "--output-dir" in run_out.out


def test_run_strict_fails_when_probes_skip(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv(
        "NODEBENCH_PROBE__PROXY__MIHOMO_PATH", str(tmp_path / "absent-mihomo")
    )
    source = write(tmp_path / "input" / "nodes.txt", URI_TEXT)
    output_dir = tmp_path / "out"
    code = main(
        [
            "run",
            "--strict",
            "--input",
            str(source),
            "--output-dir",
            str(output_dir),
        ]
    )
    out = capsys.readouterr()
    assert code == 4
    assert "status=partial" in out.out
    report = read_report(output_dir, "run-report.json")
    assert report["status"] == "partial"
    assert report["probe"]["proxy"]["skipped_reason"] == "missing_binary"


def test_probe_command_prints_summary_and_writes_results(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.setenv(
        "NODEBENCH_PROBE__PROXY__MIHOMO_PATH", str(tmp_path / "absent-mihomo")
    )
    source = write(tmp_path / "input" / "nodes.txt", URI_TEXT)
    output_dir = tmp_path / "out"
    code = main(
        [
            "probe",
            "--input",
            str(source),
            "--output-dir",
            str(output_dir),
        ]
    )
    out = capsys.readouterr()
    assert code == 4
    assert (
        "probe proxy: mode=skip backend=mihomo attempted=0 ok=0 usable_real=0 "
        "skipped=2 reason=missing_binary" in out.out
    )
    assert (
        "probe cf: mode=skip backend=cfst attempted=0 ok=0 usable_real=0 "
        "skipped=0 reason=disabled" in out.out
    )
    assert "status=partial" in out.out
    assert "probe_results=" in out.out
    payloads = list(output_dir.glob("*/probe-results.json"))
    assert len(payloads) == 1
    payload = json.loads(payloads[0].read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert len(payload["results"]) == 2
    assert all(item["status"] == "skipped" for item in payload["results"])
    assert all(item["probe_mode"] == "not_run" for item in payload["results"])
    assert all(
        item["skipped_reason"] == "missing_binary" for item in payload["results"]
    )


def test_run_cf_only_profile_executes_cf_branch(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.setenv(
        "NODEBENCH_PROBE__CF__CFST_PATH", str(tmp_path / "absent-cfst")
    )
    output_dir = tmp_path / "out"
    code = main(["run", "--profile", "cf-only", "--output-dir", str(output_dir)])
    capsys.readouterr()
    assert code == 4
    report = read_report(output_dir, "run-report.json")
    assert report["profile"] == "cf-only"
    assert report["dry_run"] is False
    assert report["probe"]["proxy"]["mode"] == "skip"
    assert report["probe"]["proxy"]["skipped_reason"] == "disabled"
    cf = report["probe"]["cf"]
    assert cf["mode"] == "skip"
    assert cf["skipped_reason"] == "missing_binary"
    assert cf["attempted"] == 0
    assert cf["ok"] == 0
    assert report["counts"]["edge_endpoints"] > 0
    payloads = list(output_dir.glob("*/probe-results.json"))
    assert len(payloads) == 1
    payload = json.loads(payloads[0].read_text(encoding="utf-8"))
    assert payload["results"]
    assert all(item["backend"] == "cfst" for item in payload["results"])
    assert all(item["status"] == "skipped" for item in payload["results"])
    assert all(item["probe_mode"] == "not_run" for item in payload["results"])
    assert all(item["attempts"] == 0 for item in payload["results"])
    assert not list(output_dir.glob("*/dry-run-report.json"))


def test_run_cf_only_profile_dry_run_exits_zero(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.chdir(PROJECT_ROOT)
    output_dir = tmp_path / "out"
    code = main(
        [
            "run",
            "--profile",
            "cf-only",
            "--dry-run",
            "--output-dir",
            str(output_dir),
        ]
    )
    capsys.readouterr()
    assert code == 0
    report = read_report(output_dir, "dry-run-report.json")
    assert report["status"] in {"ok", "partial"}
    assert report["probe"]["proxy"]["skipped_reason"] == "dry_run"
    assert report["probe"]["cf"]["skipped_reason"] == "dry_run"
    assert not list(output_dir.glob("*/probe-results.json"))


def test_run_id_help_marks_read_restore_only(capsys):
    assert main(["run", "--help"]) == 0
    run_help = capsys.readouterr().out
    assert "--run-id" in run_help
    assert "仅用于读取/恢复指定 run，不用于重复测量" in run_help
    assert main(["probe", "--help"]) == 0
    probe_help = capsys.readouterr().out
    assert "--run-id" in probe_help
    assert "仅用于读取/恢复指定 run，不用于重复测量" in probe_help


def test_run_id_defaults_to_a_fresh_run_id(tmp_path: Path, capsys, monkeypatch):
    source = write(tmp_path / "input" / "nodes.txt", URI_TEXT)
    first_dir = tmp_path / "out-one"
    second_dir = tmp_path / "out-two"
    main(["run", "--dry-run", "--input", str(source), "--output-dir", str(first_dir)])
    capsys.readouterr()
    main(["run", "--dry-run", "--input", str(source), "--output-dir", str(second_dir)])
    capsys.readouterr()
    first = read_report(first_dir, "dry-run-report.json")
    second = read_report(second_dir, "dry-run-report.json")
    assert first["run_id"] != second["run_id"]


def test_run_reuses_explicit_run_id(tmp_path: Path, capsys):
    source = write(tmp_path / "input" / "nodes.txt", URI_TEXT)
    output_dir = tmp_path / "out"
    code = main(
        [
            "run",
            "--dry-run",
            "--run-id",
            "20260101T000000Z-abcdef",
            "--input",
            str(source),
            "--output-dir",
            str(output_dir),
        ]
    )
    capsys.readouterr()
    assert code == 0
    report = read_report(output_dir, "dry-run-report.json")
    assert report["run_id"] == "20260101T000000Z-abcdef"
