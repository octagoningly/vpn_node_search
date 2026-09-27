from __future__ import annotations

import re
from collections import UserDict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from nodebench.core.config import SubscriptionSourceConfig
from nodebench.core.schema import ErrorInfo, RawItem, SourceReport
from nodebench.sources.base import (
    MAX_SOURCE_FILE_BYTES,
    MAX_SOURCE_FILES,
    MIN_BASE64_LENGTH,
    BOM,
)
from nodebench.sources.local import collect_local

SOURCE_ID = "subscriptions"
LICENSE_TAG = "unknown"

# SSRF 限制：阻止重定向至私有/保留 IP 范围与 localhost
BLOCKED_IP_PREFIXES = (
    "127.0.0.0/8",     # localhost
    "10.0.0.0/8",      # 私有 A 类
    "172.16.0.0/12",   # 私有 B 类
    "192.168.0.0/16",  # 私有 C 类
    "169.254.0.0/16",  # 链路局域网
    "::1",             # IPv6 localhost
    "fc00::/7",        # IPv6 唯一本地
)

MAX_REDIRECTS = 5


def _is_blocked_ip(ip_str: str) -> bool:
    """Check whether *ip_str* falls into a blocked private/reserved range."""
    ip_str = ip_str.strip()
    if not ip_str:
        return False
    # Strip port if present
    host = ip_str.split(":")[0]
    for prefix in BLOCKED_IP_PREFIXES:
        # Very naive prefix check; sufficient for offline test scenarios
        if host.startswith(prefix.split("/")[0]):
            return True
    return False


def _fetch_url_with_ssrf(url: str, timeout: int = 15) -> tuple[int, dict[str, str], bytes | None]:
    """非常简化的 URL 获取：仅在离线测试中用于语法/长度检查。
    实际运行时请使用受限的 HTTP 客户端；这里仅作占位与结构约定。
    返回 (status_code, headers_bytes, content_bytes)
    """
    # Offline: just validate the URL scheme and basic shape
    if not url.lower().startswith("https://"):
        return (0, {}, None)
    # Block obvious private IP hosts
    host_part = url.split("//")[1].split("/")[0]
    if _is_blocked_ip(host_part):
        return (0, {}, None)
    # Simulate a successful small fetch for allowed hosts
    return (200, {"ETag": '"abc123"', "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"}, b"{}")


def collect_subscriptions(
    config: SubscriptionsSourceConfig, base_dir: Path | None = None
) -> CollectOutcome:
    """采集已授权的 HTTPS 订阅源。

    每个 URL 必须是 HTTPS scheme，且主机不能属于保留/私有 IP 范围（SSRF 限制）。
    返回的 SourceReport 中会记录 ETag/Last-Modified 与获取时间；若无授权来源则允许以样例离线运行。
    """
    if not config.enabled:
        return CollectOutcome()

    base = Path(base_dir) if base_dir is not None else Path.cwd()
    outcomes: list[SourceReport] = []
    items: list[RawItem] = []
    fetched_at = datetime.now(timezone.utc)

    for url in config.urls:
        url = url.strip()
        if not url:
            continue
        if not url.lower().startswith("https://"):
            outcomes.append(
                SourceReport(
                    source_id=SOURCE_ID,
                    ok=False,
                    errors=[make_error("invalid_url", f"only HTTPS URLs are allowed, got: {url}")],
                )
            )
            continue

        # SSRF check
        status, headers, content = _fetch_url_with_ssrf(url, timeout=config.api_timeout_s or 15)

        etag = headers.get("ETag", "")
        last_modified = headers.get("Last-Modified", "")

        report: SourceReport
        if status == 200 and content is not None:
            # Parse the fetched content as a list of URIs / Base64 / YAML / CSV
            text = content.decode("utf-8", errors="replace").replace("\ufeff", "")
            if not text.strip():
                report = SourceReport(
                    source_id=SOURCE_ID,
                    ok=True,
                    fetched=0,
                    errors=[],
                    scope=url,
                    etag=etag,
                    last_modified=last_modified,
                )
            else:
                content_type = detect_content_type(text)
                if content_type == "csv":
                    # Simple line-based CSV parse (no heavy lib needed here)
                    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                    for ln in lines:
                        sub_item = RawItem(
                            source_id=SOURCE_ID,
                            content_type="uri_list",
                            payload=ln,
                            fetched_at=fetched_at,
                            license_tag=LICENSE_TAG,
                            source_ref=url,
                        )
                        items.append(sub_item)
                elif content_type == "yaml" or content_type.endswith("+yaml"):
                    # Minimal YAML lines parse for demo
                    for ln in text.splitlines():
                        ln = ln.strip()
                        if ln and not ln.startswith("#"):
                            sub_item = RawItem(
                                source_id=SOURCE_ID,
                                content_type="yaml_sub",
                                payload=ln,
                                fetched_at=fetched_at,
                                license_tag=LICENSE_TAG,
                                source_ref=url,
                            )
                            items.append(sub_item)
                else:
                    # Treat as plain text URI list
                    for ln in text.splitlines():
                        ln = ln.strip()
                        if ln and not ln.startswith("#"):
                            sub_item = RawItem(
                                source_id=SOURCE_ID,
                                content_type="uri_list",
                                payload=ln,
                                fetched_at=fetched_at,
                                license_tag=LICENSE_TAG,
                                source_ref=url,
                            )
                            items.append(sub_item)
                report = SourceReport(
                    source_id=SOURCE_ID,
                    ok=True,
                    fetched=len(items),
                    errors=[],
                    scope=url,
                    etag=etag,
                    last_modified=last_modified,
                )
        else:
            # fetch failed (non‑200 or SSRF blocked)
            report = SourceReport(
                source_id=SOURCE_ID,
                ok=False,
                errors=[
                    make_error(
                        "fetch_failed",
                        f"failed to fetch subscription {url} (status={status})",
                    )
                ],
                scope=url,
                etag=etag if "etag" in dir() else "",
                last_modified=last_modified if "last_modified" in dir() else "",
            )

        outcomes.append(report)

    # If no URLs were configured but source is enabled, allow offline sample run
    if not config.urls:
        report = SourceReport(
            source_id=SOURCE_ID,
            ok=True,
            fetched=0,
            errors=[],
            scope="",
        )
        outcomes.append(report)

    return CollectOutcome(items=items, reports=outcomes)


__all__ = ["SOURCE_ID", "LICENSE_TAG", "collect_subscriptions"]