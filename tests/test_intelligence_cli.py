from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodebench.cli.main import main

PROJECT_ROOT = Path(__file__).resolve().parents[1]

RUN_ID = "20260928T033700Z-abcdef"
URI_TEXT = (
    "vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.10:443"
    "?type=tcp&security=tls&host=node.example.test#sample-192-0-2-10\n"
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def make_run_artifacts(output_dir: Path, *, exit_ip: str = "203.0.113.9") -> Path:
    run_dir = output_dir / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    write(
        run_dir / "run-report.json",
        json.dumps(
            {
                "schema_version": 1,
                "run_id": RUN_ID,
                "runner_id": "local:desktop-a",
                "profile": "local",
                "generated_at": "2026-09-28T03:37:00Z",
                "status": "ok",
                "counts": {},
            }
        ),
    )
    write(
        run_dir / "probe-results.json",
        json.dumps(
            {
                "schema_version": 1,
                "run_id": RUN_ID,
                "generated_at": "2026-09-28T03:37:00Z",
                "results": [
                    {
                        "schema_version": 1,
                        "run_id": RUN_ID,
                        "runner_id": "local:desktop-a",
                        "measured_at": "2026-09-28T03:37:00Z",
                        "status": "ok",
                        "failure_stage": "unknown",
                        "probe_mode": "real",
                        "backend": "mihomo",
                        "attempts": 1,
                        "timeouts": 0,
                        "kind": "proxy_probe_result",
                        "item_id": "node-1",
                        "total_latency_ms": 120.0,
                        "download_bytes": 1048576,
                        "speed_mb_s": 2.5,
                        "speed_unit": "MB/s",
                        "proxy_exit_ip": exit_ip,
                    }
                ],
            }
        ),
    )
    return run_dir


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("NODEBENCH_"):
            monkeypatch.delenv(name, raising=False)


def test_inspect_requires_run_id(capsys):
    code = main(["inspect"])
    captured = capsys.readouterr()
    text = f"{captured.out}{captured.err}"
    assert code == 2
    assert "run-id is required" in text or "run_id_required" in text


def test_inspect_reports_unknown_when_services_unconfigured(
    tmp_path: Path, capsys, monkeypatch
):
    make_run_artifacts(tmp_path)
    monkeypatch.chdir(PROJECT_ROOT)
    code = main(
        ["inspect", "--run-id", RUN_ID, "--output-dir", str(tmp_path)]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Run ID:" in out
    assert "exit_ip=203.0.113.9" in out
    assert "country=unknown" in out
    assert "Reputation:" in out
    assert "risk=unknown" in out
    report_path = tmp_path / RUN_ID / "intelligence-report.json"
    assert report_path.is_file()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["entries"][0]["country_code"] == "unknown"
    assert payload["reputations"][0]["risk"] is None
    assert payload["reputations"][0]["status"] == "unknown"


def test_inspect_with_geo_and_reputation(
    tmp_path: Path, capsys, monkeypatch
):
    from nodebench.intelligence import exit_ip as exit_ip_mod

    class FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self, n: int = -1) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(request, timeout=None):
        if "geo.example" in request.full_url:
            return FakeResponse(
                json.dumps(
                    {
                        "status": "success",
                        "countryCode": "JP",
                        "as": "AS15169",
                        "isp": "Example",
                    }
                ).encode()
            )
        return FakeResponse(json.dumps({"score": 10}).encode())

    monkeypatch.setattr(exit_ip_mod.urllib.request, "urlopen", fake_urlopen)
    make_run_artifacts(tmp_path)
    monkeypatch.setenv("NODEBENCH_INTELLIGENCE__GEO_URL", "http://geo.example/json")
    monkeypatch.setenv("NODEBENCH_INTELLIGENCE__REPUTATION_ENABLED", "true")
    monkeypatch.setenv(
        "NODEBENCH_INTELLIGENCE__REPUTATION_URL", "http://rep.example/check"
    )
    monkeypatch.chdir(PROJECT_ROOT)
    code = main(
        ["inspect", "--run-id", RUN_ID, "--output-dir", str(tmp_path)]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "country=JP" in out
    assert "asn=AS15169" in out
    assert "risk=10" in out or "risk=10.0" in out
    report_path = tmp_path / RUN_ID / "intelligence-report.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["entries"][0]["country_code"] == "JP"
    assert payload["reputations"][0]["status"] == "ok"
    assert payload["reputations"][0]["risk"] == pytest.approx(10.0)


def test_inspect_missing_run_report(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(PROJECT_ROOT)
    code = main(
        ["inspect", "--run-id", RUN_ID, "--output-dir", str(tmp_path)]
    )
    out = capsys.readouterr()
    assert code == 2
    assert "run_report_missing" in out.out or "error" in out.err or "error" in out.out
