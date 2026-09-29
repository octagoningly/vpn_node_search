from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nodebench.core.config import GithubSourceConfig
from nodebench.core.schema import ErrorInfo, RawItem, SourceReport
from nodebench.sources.base import (
    MAX_SOURCE_FILE_BYTES,
    CollectOutcome,
    content_type_for,
    make_error,
)
from nodebench.core.http_utils import (
    DEFAULT_USER_AGENT,
    HttpError,
    controlled_get,
    is_private_host,
    offline_requested,
)

SOURCE_ID = "github"
LICENSE_TAG = "unknown"

GITHUB_API_BASE = "https://api.github.com/search/code"
GITHUB_SEARCH_URL = "https://github.com/search/"

BLOCKED_DOMAINS = (
    "127.0.0.1",
    "localhost",
    "10.",
    "172.16.",
    "172.31.",
    "192.168.",
    "169.254.",
)

MODE_REAL = "real"
MODE_OFFLINE = "offline"
CACHE_DIR_NAME = "cache"
GITHUB_CACHE_NAMESPACE = "github"


def _is_blocked_domain(host: str) -> bool:
    """Check whether the host matches a blocked domain pattern."""
    name = (host or "").lower().strip()
    for prefix in BLOCKED_DOMAINS:
        if name.startswith(prefix):
            return True
    return is_private_host(name)


def _repo_is_allowed(repo: str, allowed: list[str]) -> bool:
    if not allowed:
        return False
    cleaned = repo.strip().lower()
    for entry in allowed:
        if cleaned == entry.strip().lower():
            return True
    return False


def _sanitize_repo_ref(repo: str) -> str:
    return repo.strip().lower()


def _simulate_github_search(query: str, max_files: int = 50) -> list[dict[str, Any]]:
    """Offline模拟 GitHub 搜索结果。"""
    results: list[dict[str, Any]] = []
    lowered = query.lower()
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
            "content_type": "yaml",
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
        if lowered and not any(
            kw in lowered for kw in ["vless", "vmess", "trojan", "shadow", "filename"]
        ):
            continue
        if len(results) >= max_files:
            break
        results.append(item)
    return results


def _cache_path(base: Path, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return base / "output" / CACHE_DIR_NAME / GITHUB_CACHE_NAMESPACE / f"{digest}.json"


def _load_cache(path: Path, ttl_hours: int) -> dict[str, Any] | None:
    if ttl_hours <= 0 or not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    saved_at = raw.get("saved_at")
    if not isinstance(saved_at, str):
        return None
    try:
        moment = datetime.fromisoformat(saved_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    age = datetime.now(timezone.utc) - moment
    if age.total_seconds() > ttl_hours * 3600:
        return None
    return raw


def _save_cache(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(payload)
        payload["saved_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _resolve_token() -> str:
    for name in ("GITHUB_TOKEN", "NODEBENCH_GITHUB_TOKEN"):
        token = os.environ.get(name, "").strip()
        if token:
            return token
    return ""


def _github_headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _search_url(query: str) -> str:
    return f"{GITHUB_API_BASE}?q={urllib.parse.quote(query)}&per_page=20"


def _extract_rate_limit(error: HttpError) -> tuple[int | None, float | None]:
    headers = error.headers or {}
    remaining: int | None = None
    raw_remaining = headers.get("x-ratelimit-remaining") or headers.get("X-RateLimit-Remaining")
    if raw_remaining is not None:
        try:
            remaining = int(str(raw_remaining).strip())
        except ValueError:
            remaining = None
    retry_after: float | None = None
    raw_retry = headers.get("retry-after") or headers.get("Retry-After")
    if raw_retry is not None:
        try:
            retry_after = max(0.0, float(str(raw_retry).strip()))
        except ValueError:
            retry_after = None
    return remaining, retry_after


def _real_github_search(
    query: str,
    config: GithubSourceConfig,
    token: str,
) -> tuple[list[dict[str, Any]], ErrorInfo | None]:
    url = _search_url(query)
    headers = _github_headers(token)
    try:
        response = controlled_get(
            url,
            timeout_s=config.timeout_s,
            max_bytes=MAX_SOURCE_FILE_BYTES,
            user_agent=config.user_agent or DEFAULT_USER_AGENT,
            headers=headers,
        )
    except HttpError as err:
        remaining, retry_after = _extract_rate_limit(err)
        if err.status in (403, 429) or (remaining is not None and remaining <= 0):
            wait = retry_after if retry_after is not None else 0.0
            return [], make_error(
                "rate_limited",
                f"GitHub search rate limited (retry_after={wait})",
                retryable=True,
            )
        return [], make_error(
            "fetch_failed" if err.retryable else "search_failed",
            f"GitHub search failed: {err.message}",
            retryable=err.retryable,
        )

    if response.rate_limit_remaining is not None and response.rate_limit_remaining <= 0:
        wait = response.retry_after if response.retry_after is not None else 0.0
        return [], make_error(
            "rate_limited",
            f"GitHub search rate limited (retry_after={wait})",
            retryable=True,
        )

    try:
        payload = json.loads(response.text)
    except ValueError as err:
        return [], make_error("search_invalid_json", f"GitHub search returned invalid JSON: {err}")

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return [], make_error("search_invalid_shape", "GitHub search response missing items")
    results: list[dict[str, Any]] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        repo_obj = entry.get("repository") if isinstance(entry.get("repository"), dict) else {}
        full_name = str(repo_obj.get("full_name") or entry.get("repository") or "")
        filename = str(entry.get("name") or entry.get("path") or "")
        size = int(entry.get("size") or 0)
        content_url = str(entry.get("url") or "")
        results.append(
            {
                "filename": filename,
                "repository": full_name,
                "size": size,
                "content_url": content_url,
                "content_type": "",
            }
        )
    return results, None


def _fetch_file_text(
    content_url: str,
    config: GithubSourceConfig,
    token: str,
) -> tuple[str | None, ErrorInfo | None]:
    if not content_url:
        return None, make_error("missing_content_url", "search hit has no content URL")
    headers = _github_headers(token)
    try:
        response = controlled_get(
            content_url,
            timeout_s=config.timeout_s,
            max_bytes=MAX_SOURCE_FILE_BYTES,
            user_agent=config.user_agent or DEFAULT_USER_AGENT,
            headers=headers,
        )
    except HttpError as err:
        return None, make_error(
            "fetch_failed",
            f"failed to fetch file content: {err.message}",
            retryable=err.retryable,
        )
    try:
        payload = json.loads(response.text)
    except ValueError:
        return response.text, None
    if isinstance(payload, dict):
        encoding = str(payload.get("encoding") or "")
        if encoding == "base64":
            import base64

            raw = payload.get("content") or ""
            try:
                return base64.b64decode(raw).decode("utf-8", errors="replace"), None
            except (ValueError, TypeError):
                return None, make_error("decode_failed", "failed to decode base64 file content")
        if "content" in payload and isinstance(payload.get("content"), str):
            return str(payload["content"]), None
    return response.text, None


def collect_github(
    config: GithubSourceConfig, base_dir: Path | None = None, secrets: dict[str, str] | None = None
) -> CollectOutcome:
    """Collect from the GitHub code-search source (real API or offline sim).

    Real mode: api.github.com/search/code with Bearer token.
    Offline: NODEBENCH_OFFLINE=1, config.offline, missing token, or
    network failure degrade to simulation and report mode=offline.
    """
    if not config.enabled:
        return CollectOutcome()

    base = Path(base_dir) if base_dir is not None else Path.cwd()
    fetched_at = datetime.now(timezone.utc)
    items: list[RawItem] = []
    reports: list[SourceReport] = []

    token = _resolve_token()
    if secrets:
        token = token or str(secrets.get("github_token") or "").strip()
    license_tag = (str(config.license_tag or "") or LICENSE_TAG).strip() or LICENSE_TAG

    want_offline = bool(config.offline) or offline_requested()
    force_offline = want_offline or not token

    if force_offline and not want_offline and not token:
        reports.append(
            SourceReport(
                source_id=SOURCE_ID,
                ok=False,
                fetched=0,
                errors=[
                    make_error(
                        "missing_token",
                        "GITHUB_TOKEN is not set; degrading to offline simulation",
                        retryable=True,
                    )
                ],
                scope="; ".join(config.queries),
                mode=MODE_OFFLINE,
            )
        )
    elif want_offline:
        reports.append(
            SourceReport(
                source_id=SOURCE_ID,
                ok=True,
                fetched=0,
                errors=[
                    make_error(
                        "offline_mode",
                        "offline mode requested; using simulated search results",
                        retryable=False,
                    )
                ],
                scope="; ".join(config.queries),
                mode=MODE_OFFLINE,
            )
        )

    used_offline = force_offline
    total_items = 0
    rate_limited = False

    for query in config.queries:
        query = query.strip()
        if not query:
            continue

        results: list[dict[str, Any]] = []
        query_error: ErrorInfo | None = None
        mode = MODE_OFFLINE if used_offline else MODE_REAL

        if not used_offline:
            cache_file = _cache_path(base, f"{query}|{','.join(config.allowed_repositories)}")
            cached = _load_cache(cache_file, config.cache_ttl_hours)
            if cached and isinstance(cached.get("results"), list):
                results = list(cached["results"])
            else:
                real_results, query_error = _real_github_search(query, config, token)
                if query_error is None:
                    results = real_results
                    _save_cache(cache_file, {"query": query, "results": results})
                elif query_error.code == "rate_limited":
                    rate_limited = True
                    used_offline = False
                else:
                    # network/API failure → offline degradation
                    used_offline = True
                    mode = MODE_OFFLINE
                    results = _simulate_github_search(query, max_files=config.max_files_per_run)
                    reports.append(
                        SourceReport(
                            source_id=SOURCE_ID,
                            ok=False,
                            fetched=0,
                            errors=[
                                query_error,
                                make_error(
                                    "offline_fallback",
                                    "network failure; falling back to offline simulation",
                                    retryable=True,
                                ),
                            ],
                            scope=query,
                            mode=MODE_OFFLINE,
                        )
                    )

        if used_offline and not results and query_error is None:
            results = _simulate_github_search(query, max_files=config.max_files_per_run)
            mode = MODE_OFFLINE

        query_items = 0
        for res in results:
            if total_items >= config.max_files_per_run:
                break
            repo = str(res.get("repository") or "")
            if not _repo_is_allowed(repo, config.allowed_repositories):
                continue
            host_candidate = repo.split("/")[0] if "/" in repo else repo
            if _is_blocked_domain(host_candidate):
                continue

            filename = str(res.get("filename") or "")
            content_url = str(res.get("content_url") or "")
            text: str | None = None
            if not used_offline and content_url:
                text, fetch_err = _fetch_file_text(content_url, config, token)
                if fetch_err is not None and text is None:
                    continue
            if text is None:
                text = filename or ""

            ctype = res.get("content_type") or ""
            if ctype in {"uri_list", "base64_sub", "yaml", "csv", "text"}:
                content_type = ctype
            else:
                content_type = content_type_for(Path(filename or "item.txt"), text)

            item = RawItem(
                source_id=SOURCE_ID,
                content_type=content_type,
                payload=text,
                fetched_at=fetched_at,
                license_tag=license_tag,
                source_ref=f"{repo}/{filename}" if repo else filename,
            )
            items.append(item)
            query_items += 1
            total_items += 1

        if rate_limited:
            report = SourceReport(
                source_id=SOURCE_ID,
                ok=False,
                fetched=query_items,
                errors=[
                    make_error(
                        "rate_limited",
                        "GitHub API rate limited; obeying Retry-After",
                        retryable=True,
                    )
                ],
                scope=query,
                mode=MODE_REAL,
            )
        elif used_offline and query_error is not None:
            # already reported above
            report = None
        else:
            errors: list[ErrorInfo] = []
            if query_error is not None:
                errors.append(query_error)
            report = SourceReport(
                source_id=SOURCE_ID,
                ok=query_items > 0 or not errors,
                fetched=query_items,
                errors=errors,
                scope=query,
                mode=mode,
            )
        if report is not None:
            reports.append(report)

    if not config.queries:
        reports.append(
            SourceReport(
                source_id=SOURCE_ID,
                ok=True,
                fetched=0,
                errors=[],
                scope="",
                mode=MODE_OFFLINE if used_offline else MODE_REAL,
            )
        )

    return CollectOutcome(items=items, reports=reports)


__all__ = [
    "SOURCE_ID",
    "LICENSE_TAG",
    "BLOCKED_DOMAINS",
    "MODE_REAL",
    "MODE_OFFLINE",
    "collect_github",
    "_is_blocked_domain",
    "_simulate_github_search",
]
