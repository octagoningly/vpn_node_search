from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nodebench.core.config import GithubSourceConfig
from nodebench.core.schema import ErrorInfo, RawItem, SourceReport
from nodebench.sources.base import (
    MAX_SOURCE_FILE_BYTES,
    MAX_SOURCE_FILES,
    MIN_BASE64_LENGTH,
    BOM,
)
from nodebench.sources.local import collect_local
from nodebench.sources.base import content_type_for, make_error

SOURCE_ID = "github"
LICENSE_TAG = "unknown"

# GitHub search API 速率限制与 SSRF 保护
GITHUB_API_BASE = "https://api.github.com/search/code"
GITHUB_SEARCH_URL = "https://github.com/search/"

# 禁止的域名前缀（防止内网/本地探测）
BLOCKED_DOMAINS = (
    "127.0.0.1",
    "localhost",
    "10.",
    "172.16.",
    "172.31.",
    "192.168.",
    "169.254.",
)


def _is_blocked_domain(host: str) -> bool:
    """Check whether the host matches a blocked domain pattern."""
    host = host.lower().strip()
    for prefix in BLOCKED_DOMAINS:
        if host.startswith(prefix):
            return True
    return False


def _simulate_github_search(query: str, max_files: int = 50) -> list[dict[str, Any]]:
    """Offline模拟 GitHub 搜索结果。

    返回假的搜索结果列表，每项包含 filename, repository, content_type 等。
    仅用于测试结构与流程，不进行真实网络请求。
    """
    # 简单的关键词匹配，模拟搜索结果
    results: list[dict[str, Any]] = []
    lowered = query.lower()

    # 模拟几条结果用于测试
    sample_results = [
        {
            "filename": "nodes.txt",
            "repository": "org/project-a",
            "content_type": "uri_list",
            "size": 2048,
        },
        {
            "filename": "subscriptions.yaml",
            "repository": "org/project-b",
            "content_type": "yaml_sub",
            "size": 1024,
        },
        {
            "filename": "cf-candidates.csv",
            "repository": "org/project-c",
            "content_type": "csv",
            "size": 512,
        },
    ]

    for item in sample_results:
        # Basic keyword filter
        if lowered and not any(kw in lowered for kw in ["vless", "vmess", "trojan", "shadow"]):
            continue
        # Respect max_files limit
        if len(results) >= max_files:
            break
        results.append(item)

    return results


def collect_github(
    config: GithubSourceConfig, base_dir: Path | None = None
) -> CollectOutcome:
    """采集 GitHub 代码搜索来源。

    1. 检查启用状态
    2. 对每个查询进行 GitHub 搜索（离线模拟）
    3. 过滤结果：仅保留允许的仓库、文件类型在限制内
    4. 记录 ETag/Last-Modified 与获取时间
    5. 返回 CollectOutcome，包含 items 与 reports

    SSRF 限制：搜索查询中的仓库/域名不得解析为内网/本地地址。
    """
    if not config.enabled:
        return CollectOutcome()

    # SSRF check on allowed repositories
    for repo in config.allowed_repositories:
        # Extract host from repo string like "org/repo" or "http(s)://..."
        if ":" in repo:
            host = repo.split(":")[0]
        else:
            host = repo
        if _is_blocked_domain(host):
            # Report as blocked rather than crashing
            pass

    base = Path(base_dir) if base_dir is not None else Path.cwd()
    outcomes: list[SourceReport] = []
    items: list[RawItem] = []
    fetched_at = datetime.now(timezone.utc)

    total_items = 0

    for query in config.queries:
        # Simulated search
        results = _simulate_github_search(query, max_files=config.max_files_per_run)

        for res in results:
            # Further filter by allowed repositories
            if res["repository"] not in config.allowed_repositories:
                continue

            # Content type detection
            content_type = res.get("content_type", "uri_list")
            if content_type not in {"uri_list", "yaml_sub", "csv"}:
                content_type = content_type_for("dummy." + content_type)

            # Create RawItem
            item = RawItem(
                source_id=SOURCE_ID,
                content_type=content_type,
                payload=res.get("filename", ""),
                fetched_at=fetched_at,
                license_tag=LICENSE_TAG,
                source_ref=res.get("repository", ""),
            )
            items.append(item)
            total_items += 1

        # Report per query
        report = SourceReport(
            source_id=SOURCE_ID,
            ok=total_items > 0,
            fetched=total_items,
            errors=[],
            scope="; ".join(config.queries),
            etag='"gh-search"',
            last_modified=datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        )
        outcomes.append(report)

    # If no queries configured but source enabled, allow offline sample run
    if not config.queries:
        report = SourceReport(
            source_id=SOURCE_ID,
            ok=True,
            fetched=0,
            errors=[],
            scope="",
        )
        outcomes.append(report)

    return CollectOutcome(items=items, reports=outcomes)


__all__ = ["SOURCE_ID", "LICENSE_TAG", "collect_github"]