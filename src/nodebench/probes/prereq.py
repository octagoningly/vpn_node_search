from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from nodebench.core.config import AppConfig
from nodebench.probes.base import (
    CFST_BINARY_NAMES,
    MIHOMO_BINARY_NAMES,
    REASON_DISABLED,
    REASON_MISSING_BINARY,
    REASON_MISSING_SPEEDTEST_URL,
    REASON_MISSING_TARGET_HOST,
    resolve_binary,
)

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_FAIL = "fail"
LEVEL_INFO = "info"


@dataclass(frozen=True)
class PrereqReport:
    name: str
    ok: bool
    level: str
    detail: str
    fix: str = ""
    reason: str = ""


def check_proxy_prereqs(
    config: AppConfig, root: Path | None = None
) -> list[PrereqReport]:
    probe = config.probe.proxy
    if not probe.enabled:
        return [
            PrereqReport(
                name="mihomo_binary",
                ok=True,
                level=LEVEL_INFO,
                detail="probe proxy disabled",
                reason=REASON_DISABLED,
            ),
            PrereqReport(
                name="speedtest_url",
                ok=True,
                level=LEVEL_INFO,
                detail="probe proxy disabled",
                reason=REASON_DISABLED,
            ),
        ]
    reports: list[PrereqReport] = []
    binary = resolve_binary(probe.mihomo_path, MIHOMO_BINARY_NAMES, root)
    if binary:
        reports.append(
            PrereqReport(
                name="mihomo_binary",
                ok=True,
                level=LEVEL_OK,
                detail=binary,
            )
        )
    else:
        reports.append(
            PrereqReport(
                name="mihomo_binary",
                ok=False,
                level=LEVEL_FAIL,
                detail="mihomo binary not found",
                fix="set probe.proxy.mihomo_path to a project-local mihomo binary",
                reason=REASON_MISSING_BINARY,
            )
        )
    speedtest_url = (probe.speedtest_url or "").strip()
    if speedtest_url:
        reports.append(
            PrereqReport(
                name="speedtest_url",
                ok=True,
                level=LEVEL_OK,
                detail=speedtest_url,
            )
        )
    else:
        reports.append(
            PrereqReport(
                name="speedtest_url",
                ok=False,
                level=LEVEL_WARN,
                detail="probe.proxy.speedtest_url is empty; probing stays pending",
                fix="set probe.proxy.speedtest_url before enabling probing",
                reason=REASON_MISSING_SPEEDTEST_URL,
            )
        )
    return reports


def check_cf_prereqs(config: AppConfig, root: Path | None = None) -> list[PrereqReport]:
    probe = config.probe.cf
    if not probe.enabled:
        return [
            PrereqReport(
                name="cfst_binary",
                ok=True,
                level=LEVEL_INFO,
                detail="probe cf disabled",
                reason=REASON_DISABLED,
            ),
            PrereqReport(
                name="cf_target_host",
                ok=True,
                level=LEVEL_INFO,
                detail="probe cf disabled",
                reason=REASON_DISABLED,
            ),
        ]
    reports = []
    binary = resolve_binary(probe.cfst_path, CFST_BINARY_NAMES, root)
    if binary:
        reports.append(
            PrereqReport(
                name="cfst_binary", ok=True, level=LEVEL_OK, detail=binary
            )
        )
    else:
        reports.append(
            PrereqReport(
                name="cfst_binary",
                ok=False,
                level=LEVEL_FAIL,
                detail="cloudflareSpeedTest binary not found",
                fix=(
                    "set probe.cf.cfst_path to a project-local "
                    "cloudflareSpeedTest binary"
                ),
                reason=REASON_MISSING_BINARY,
            )
        )
    target_host = (probe.target_host or "").strip()
    if target_host:
        reports.append(
            PrereqReport(
                name="cf_target_host", ok=True, level=LEVEL_OK, detail=target_host
            )
        )
    else:
        reports.append(
            PrereqReport(
                name="cf_target_host",
                ok=False,
                level=LEVEL_FAIL,
                detail="probe.cf.target_host is empty while cf is enabled",
                fix="set probe.cf.target_host or disable probe.cf",
                reason=REASON_MISSING_TARGET_HOST,
            )
        )
    return reports


def first_reason(reports: Sequence[PrereqReport]) -> str:
    for report in reports:
        if report.reason:
            return report.reason
    return ""


def as_check(report: PrereqReport) -> tuple[str, str, str, str]:
    return (report.level, report.name, report.detail, report.fix)


__all__ = [
    "LEVEL_FAIL",
    "LEVEL_INFO",
    "LEVEL_OK",
    "LEVEL_WARN",
    "PrereqReport",
    "as_check",
    "check_cf_prereqs",
    "check_proxy_prereqs",
    "first_reason",
]
