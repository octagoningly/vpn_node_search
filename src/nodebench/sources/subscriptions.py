from __future__ import annotations

import hashlib
import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nodebench.core.config import SubscriptionSourceConfig
from nodebench.core.schema import ErrorInfo, RawItem, SourceReport
from nodebench.sources.base import (
    MAX_SOURCE_FILE_BYTES,
    BOM,
    CollectOutcome,
    content_type_for,
    make_error,
)
from nodebench.sources.http_utils import (
    DEFAULT_MAX_BYTES,
    DEFAULT_USER_AGENT,
    HttpError,
    controlled_get,
    is_private_host,
    offline_requested,
    scrub_url,
)

SOURCE_ID = "subscriptions"
LICENSE_TAG = "unknown"

MODE_REAL = "real"
MODE_OFFLINE = "offline"
CACHE_DIR_NAME = "cache"
SUBSCRIPTIONS_CACHE_NAMESPACE = "subscriptions"

BLOCKED_IP_PREFIXES = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "::1",
    "fc00::/7",
)

MAX_REDIRECTS = 5


def _is_blocked_ip(ip_str: str) -> bool:
    """Check whether *ip_str* falls into a blocked private/reserved range."""
    return is_private_host(ip_str)


def _url_ref(url: str) -> str:
    """Opaque short reference that never echoes the URL itself."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"sub:{digest}"


def _cache_path(base: Path, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return base / "output" / CACHE_DIR_NAME / SUBSCRIPTIONS_CACHE_NAMESPACE / f"{digest}.json"


def _load_cache(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _save_cache(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def collect_subscriptions(
    config: SubscriptionSourceConfig, base_dir: Path | None = None
) -> CollectOutcome:
    """采集已授权的 HTTPS 订阅源。

    - 只读取显式列出的 HTTPS URL；拒绝私有/保留主机。
    - 记录 ETag/Last-Modified；304 时复用缓存。
    - 单源失败隔离；成功缓存不会被失败覆盖。
    - 原文整体作为 RawItem 交下游解析。
    - 报告不回显完整 URL（只使用不透明引用）。
    - 离线（urls 为空 / NODEBENCH_OFFLINE / config.offline）不发起网络请求。
    """
    if not config.enabled:
        return CollectOutcome()

    base = Path(base_dir) if base_dir is not None else Path.cwd()
    outcomes: list[SourceReport] = []
    items: list[RawItem] = []
    fetched_at = datetime.now(timezone.utc)
    license_tag = (config.license_tag or LICENSE_TAG).strip() or LICENSE_TAG

    urls = [u.strip() for u in config.urls if u and u.strip()]
    offline = bool(config.offline) or offline_requested()

    if not urls:
        mode = MODE_OFFLINE if offline else MODE_REAL
        outcomes.append(
            SourceReport(
                source_id=SOURCE_ID,
                ok=True,
                fetched=0,
                errors=[
                    make_error(
                        "no_urls",
                        "no subscription URLs configured; nothing to fetch",
                        retryable=False,
                    )
                ],
                scope="",
                mode=mode,
            )
        )
        return CollectOutcome(items=items, reports=outcomes)

    if offline:
        outcomes.append(
            SourceReport(
                source_id=SOURCE_ID,
                ok=True,
                fetched=0,
                errors=[
                    make_error(
                        "offline_mode",
                        "offline mode requested; skipping subscription fetch",
                        retryable=False,
                    )
                ],
                scope="",
                mode=MODE_OFFLINE,
            )
        )
        return CollectOutcome(items=items, reports=outcomes)

    for url in urls:
        ref = _url_ref(url)
        scheme_ok = url.lower().startswith("https://")
        if not scheme_ok:
            outcomes.append(
                SourceReport(
                    source_id=SOURCE_ID,
                    ok=False,
                    fetched=0,
                    errors=[
                        make_error(
                            "invalid_url",
                            "only HTTPS URLs are allowed",
                            retryable=False,
                        )
                    ],
                    scope=ref,
                    mode=MODE_REAL,
                )
            )
            continue

        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        if not host or is_private_host(host):
            outcomes.append(
                SourceReport(
                    source_id=SOURCE_ID,
                    ok=False,
                    fetched=0,
                    errors=[
                        make_error(
                            "blocked_host",
                            "subscription host is not allowed",
                            retryable=False,
                        )
                    ],
                    scope=ref,
                    mode=MODE_REAL,
                )
            )
            continue

        cache_file = _cache_path(base, url)
        cached = _load_cache(cache_file)
        etag = str((cached or {}).get("etag") or "")
        last_modified = str((cached or {}).get("last_modified") or "")

        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        try:
            response = controlled_get(
                url,
                timeout_s=config.timeout_s,
                max_bytes=min(MAX_SOURCE_FILE_BYTES, DEFAULT_MAX_BYTES),
                user_agent=config.user_agent or DEFAULT_USER_AGENT,
                headers=headers,
                max_redirects=MAX_REDIRECTS,
            )
        except HttpError as err:
            # Failure must not overwrite last good cache.
            errors = [
                make_error(
                    err.code,
                    f"subscription fetch failed: {scrub_url(err.message)}",
                    retryable=err.retryable,
                )
            ]
            mode = MODE_OFFLINE if offline_requested() or config.offline else MODE_REAL
            if cached and cached.get("payload"):
                errors.append(
                    make_error(
                        "stale_cache",
                        "retaining last successful cached content (stale, not for publish)",
                        retryable=True,
                    )
                )
                text = str(cached.get("payload") or "").removeprefix(BOM)
                content_type = content_type_for(Path("sub.txt"), text)
                items.append(
                    RawItem(
                        source_id=SOURCE_ID,
                        content_type=content_type,
                        payload=text,
                        fetched_at=fetched_at,
                        license_tag=license_tag,
                        source_ref=ref,
                    )
                )
                outcomes.append(
                    SourceReport(
                        source_id=SOURCE_ID,
                        ok=False,
                        fetched=1,
                        errors=errors,
                        scope=ref,
                        mode=mode,
                        etag=etag,
                        last_modified=last_modified,
                    )
                )
            else:
                outcomes.append(
                    SourceReport(
                        source_id=SOURCE_ID,
                        ok=False,
                        fetched=0,
                        errors=errors,
                        scope=ref,
                        mode=mode,
                    )
                )
            continue

        new_etag = response.etag or etag
        new_last_modified = response.last_modified or last_modified

        if response.not_modified:
            text = str((cached or {}).get("payload") or "").removeprefix(BOM)
            if text:
                content_type = content_type_for(Path("sub.txt"), text)
                items.append(
                    RawItem(
                        source_id=SOURCE_ID,
                        content_type=content_type,
                        payload=text,
                        fetched_at=fetched_at,
                        license_tag=license_tag,
                        source_ref=ref,
                    )
                )
                outcomes.append(
                    SourceReport(
                        source_id=SOURCE_ID,
                        ok=True,
                        fetched=1,
                        errors=[],
                        scope=ref,
                        mode=MODE_REAL,
                        etag=new_etag,
                        last_modified=new_last_modified,
                    )
                )
            else:
                outcomes.append(
                    SourceReport(
                        source_id=SOURCE_ID,
                        ok=True,
                        fetched=0,
                        errors=[],
                        scope=ref,
                        mode=MODE_REAL,
                        etag=new_etag,
                        last_modified=new_last_modified,
                    )
                )
            continue

        text = response.text.removeprefix(BOM)
        _save_cache(
            cache_file,
            {
                "etag": new_etag,
                "last_modified": new_last_modified,
                "payload": text,
            },
        )

        if not text.strip():
            outcomes.append(
                SourceReport(
                    source_id=SOURCE_ID,
                    ok=True,
                    fetched=0,
                    errors=[],
                    scope=ref,
                    mode=MODE_REAL,
                    etag=new_etag,
                    last_modified=new_last_modified,
                )
            )
            continue

        content_type = content_type_for(Path("sub.txt"), text)
        items.append(
            RawItem(
                source_id=SOURCE_ID,
                content_type=content_type,
                payload=text,
                fetched_at=fetched_at,
                license_tag=license_tag,
                source_ref=ref,
            )
        )
        outcomes.append(
            SourceReport(
                source_id=SOURCE_ID,
                ok=True,
                fetched=1,
                errors=[],
                scope=ref,
                mode=MODE_REAL,
                etag=new_etag,
                last_modified=new_last_modified,
            )
        )

    return CollectOutcome(items=items, reports=outcomes)


__all__ = [
    "SOURCE_ID",
    "LICENSE_TAG",
    "MAX_REDIRECTS",
    "MODE_REAL",
    "MODE_OFFLINE",
    "collect_subscriptions",
    "_is_blocked_ip",
]
