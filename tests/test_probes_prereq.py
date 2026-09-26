from __future__ import annotations

import shutil
from pathlib import Path

from nodebench.core.config import AppConfig, load_config
from nodebench.probes import (
    CFST_BINARY_NAMES,
    LEVEL_FAIL,
    LEVEL_INFO,
    LEVEL_OK,
    LEVEL_WARN,
    MIHOMO_BINARY_NAMES,
    REASON_DISABLED,
    REASON_MISSING_BINARY,
    REASON_MISSING_SPEEDTEST_URL,
    REASON_MISSING_TARGET_HOST,
    as_check,
    check_cf_prereqs,
    check_proxy_prereqs,
    first_reason,
    resolve_binary,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_PATH = CONFIG_DIR / "default.yaml"


def base_config() -> AppConfig:
    return load_config(DEFAULT_PATH, None, env={})


def test_resolve_binary_prefers_configured_absolute_path(tmp_path: Path):
    fake = tmp_path / "mihomo"
    fake.write_bytes(b"")
    assert resolve_binary(str(fake), MIHOMO_BINARY_NAMES) == str(fake)


def test_resolve_binary_resolves_relative_path_against_root(tmp_path: Path):
    (tmp_path / "bin").mkdir()
    fake = tmp_path / "bin" / "mihomo"
    fake.write_bytes(b"")
    assert resolve_binary("bin/mihomo", MIHOMO_BINARY_NAMES, tmp_path) == str(fake)
    assert resolve_binary("bin/absent", MIHOMO_BINARY_NAMES, tmp_path) is None


def test_resolve_binary_configured_path_never_falls_back_to_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/found/{name}")
    absent = str(PROJECT_ROOT / "missing" / "mihomo")
    assert resolve_binary(absent, MIHOMO_BINARY_NAMES) is None


def test_resolve_binary_empty_configuration_searches_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/found/{name}")
    assert resolve_binary("", MIHOMO_BINARY_NAMES) == "/found/mihomo"
    assert resolve_binary("", CFST_BINARY_NAMES) == "/found/cloudflareSpeedTest"
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert resolve_binary("", MIHOMO_BINARY_NAMES) is None


def test_check_proxy_prereqs_disabled_reports_info():
    config = base_config()
    config.probe.proxy.enabled = False
    reports = check_proxy_prereqs(config)
    assert [report.name for report in reports] == ["mihomo_binary", "speedtest_url"]
    assert all(report.ok for report in reports)
    assert all(report.level == LEVEL_INFO for report in reports)
    assert all(report.reason == REASON_DISABLED for report in reports)
    assert first_reason(reports) == REASON_DISABLED


def test_check_proxy_prereqs_reports_missing_binary_and_url(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    config = base_config()
    config.probe.proxy.mihomo_path = str(tmp_path / "absent-mihomo")
    config.probe.proxy.speedtest_url = ""
    reports = check_proxy_prereqs(config)
    binary, url = reports
    assert binary.ok is False
    assert binary.level == LEVEL_FAIL
    assert binary.reason == REASON_MISSING_BINARY
    assert binary.fix
    assert url.ok is False
    assert url.level == LEVEL_WARN
    assert url.reason == REASON_MISSING_SPEEDTEST_URL
    assert url.fix
    assert first_reason(reports) == REASON_MISSING_BINARY
    assert as_check(binary) == (
        LEVEL_FAIL,
        "mihomo_binary",
        "mihomo binary not found",
        binary.fix,
    )


def test_check_proxy_prereqs_ready(tmp_path: Path):
    fake = tmp_path / "mihomo"
    fake.write_bytes(b"")
    config = base_config()
    config.probe.proxy.mihomo_path = str(fake)
    config.probe.proxy.speedtest_url = "https://speed.cloudflare.com/__down"
    reports = check_proxy_prereqs(config)
    assert all(report.ok for report in reports)
    assert all(report.level == LEVEL_OK for report in reports)
    assert first_reason(reports) == ""
    assert as_check(reports[0]) == (LEVEL_OK, "mihomo_binary", str(fake), "")
    assert as_check(reports[1]) == (
        LEVEL_OK,
        "speedtest_url",
        "https://speed.cloudflare.com/__down",
        "",
    )


def test_check_cf_prereqs_disabled_reports_info():
    config = base_config()
    reports = check_cf_prereqs(config)
    assert [report.name for report in reports] == ["cfst_binary", "cf_target_host"]
    assert all(report.ok for report in reports)
    assert all(report.level == LEVEL_INFO for report in reports)
    assert all(report.reason == REASON_DISABLED for report in reports)


def test_check_cf_prereqs_reports_missing_binary_and_target_host(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    config = base_config()
    config.probe.cf.enabled = True
    config.probe.cf.cfst_path = str(tmp_path / "absent-cfst")
    config.probe.cf.target_host = ""
    reports = check_cf_prereqs(config)
    binary, host = reports
    assert binary.ok is False
    assert binary.level == LEVEL_FAIL
    assert binary.reason == REASON_MISSING_BINARY
    assert binary.fix
    assert host.ok is False
    assert host.level == LEVEL_FAIL
    assert host.reason == REASON_MISSING_TARGET_HOST
    assert host.fix
    assert first_reason(reports) == REASON_MISSING_BINARY


def test_check_cf_prereqs_ready(tmp_path: Path):
    fake = tmp_path / "cloudflareSpeedTest"
    fake.write_bytes(b"")
    config = base_config()
    config.probe.cf.enabled = True
    config.probe.cf.cfst_path = str(fake)
    config.probe.cf.target_host = "edge.example.test"
    reports = check_cf_prereqs(config)
    assert all(report.ok for report in reports)
    assert all(report.level == LEVEL_OK for report in reports)
    assert first_reason(reports) == ""
    assert as_check(reports[0]) == (LEVEL_OK, "cfst_binary", str(fake), "")
    assert as_check(reports[1]) == (
        LEVEL_OK,
        "cf_target_host",
        "edge.example.test",
        "",
    )
