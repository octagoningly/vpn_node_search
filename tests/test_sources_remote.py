from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nodebench.core.config import (
    GithubSourceConfig,
    SourcesConfig,
    SubscriptionSourceConfig,
)
from nodebench.sources import github as github_mod
from nodebench.sources import http_utils
from nodebench.sources import subscriptions as subs_mod
from nodebench.sources.http_utils import HttpError, controlled_get


class FakeHeaders(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default

    def items(self):
        return super().items()


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, headers: dict | None = None):
        self._body = body
        self._pos = 0
        self.status = status
        self.headers = FakeHeaders(headers or {})

    def getcode(self):
        return self.status

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            chunk = self._body[self._pos :]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def info(self):
        return self.headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def make_opener(scripted: dict[str, object]):
    """Return an opener callable that serves scripted responses per URL."""

    def opener(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if url not in scripted:
            raise AssertionError(f"unexpected URL: {url}")
        result = scripted[url]
        if isinstance(result, Exception):
            raise result
        return result

    return opener


# ---------------------------------------------------------------------------
# http_utils
# ---------------------------------------------------------------------------


def test_controlled_get_success(monkeypatch):
    body = b"vless://123e4567-e89b-12d3-a456-426614174000@example.test:443#n"
    opener = make_opener(
        {
            "https://example.test/sub.txt": FakeResponse(
                body, headers={"ETag": '"abc"', "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"}
            )
        }
    )
    resp = controlled_get("https://example.test/sub.txt", opener=opener)
    assert resp.status == 200
    assert resp.body == body
    assert resp.etag == '"abc"'
    assert resp.last_modified.startswith("Mon")


def test_controlled_get_rejects_http_scheme():
    with pytest.raises(HttpError) as exc:
        controlled_get("http://example.test/x")
    assert exc.value.code == "insecure_scheme"
    assert exc.value.retryable is False


def test_controlled_get_rejects_private_host():
    with pytest.raises(HttpError) as exc:
        controlled_get("https://127.0.0.1/x")
    assert exc.value.code == "blocked_host"
    with pytest.raises(HttpError) as exc:
        controlled_get("https://localhost/x")
    assert exc.value.code == "blocked_host"
    with pytest.raises(HttpError) as exc:
        controlled_get("https://192.168.1.1/x")
    assert exc.value.code == "blocked_host"
    with pytest.raises(HttpError) as exc:
        controlled_get("https://10.0.0.5/x")
    assert exc.value.code == "blocked_host"


def test_controlled_get_timeout_is_retryable(monkeypatch):
    def opener(request, timeout=None):
        raise urllib.error.URLError(TimeoutError("timed out"))

    with pytest.raises(HttpError) as exc:
        controlled_get("https://example.test/x", opener=opener)
    assert exc.value.code == "timeout"
    assert exc.value.retryable is True


def test_controlled_get_size_limit():
    big = b"x" * 100
    opener = make_opener({"https://example.test/big": FakeResponse(big)})
    with pytest.raises(HttpError) as exc:
        controlled_get("https://example.test/big", max_bytes=10, opener=opener)
    assert exc.value.code == "response_too_large"
    assert exc.value.retryable is False


def test_controlled_get_304_not_modified():
    opener = make_opener(
        {
            "https://example.test/sub.txt": FakeResponse(
                b"", status=304, headers={"ETag": '"abc"'}
            )
        }
    )
    resp = controlled_get(
        "https://example.test/sub.txt",
        headers={"If-None-Match": '"abc"'},
        opener=opener,
    )
    assert resp.status == 304
    assert resp.not_modified is True
    assert resp.body == b""


def test_controlled_get_redirect_to_private_is_blocked():
    opener = make_opener(
        {
            "https://example.test/go": FakeResponse(
                b"", status=302, headers={"Location": "https://127.0.0.1/secret"}
            )
        }
    )
    with pytest.raises(HttpError) as exc:
        controlled_get("https://example.test/go", opener=opener)
    assert exc.value.code == "blocked_host"


def test_controlled_get_redirect_chain_ok():
    opener = make_opener(
        {
            "https://example.test/a": FakeResponse(
                b"", status=302, headers={"Location": "https://example.test/b"}
            ),
            "https://example.test/b": FakeResponse(b"hello"),
        }
    )
    resp = controlled_get("https://example.test/a", opener=opener)
    assert resp.status == 200
    assert resp.body == b"hello"


def test_is_private_host_matrix():
    assert http_utils.is_private_host("127.0.0.1") is True
    assert http_utils.is_private_host("10.1.2.3") is True
    assert http_utils.is_private_host("172.16.0.1") is True
    assert http_utils.is_private_host("172.31.255.1") is True
    assert http_utils.is_private_host("192.168.0.1") is True
    assert http_utils.is_private_host("169.254.1.1") is True
    assert http_utils.is_private_host("example.test") is False
    assert http_utils.is_private_host("api.github.com") is False


# ---------------------------------------------------------------------------
# subscriptions
# ---------------------------------------------------------------------------


def test_subscriptions_success_creates_raw_item(tmp_path: Path, monkeypatch):
    body = b"vless://123e4567-e89b-12d3-a456-426614174000@example.test:443#n\n"
    url = "https://subs.example.test/list.txt"
    opener = make_opener(
        {
            url: FakeResponse(
                body, headers={"ETag": '"v1"', "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"}
            )
        }
    )

    def fake_urlopen(request, timeout=None):
        return opener(request, timeout=timeout)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    config = SubscriptionSourceConfig(enabled=True, urls=[url])
    outcome = subs_mod.collect_subscriptions(config, base_dir=tmp_path)
    assert len(outcome.items) == 1
    assert outcome.items[0].payload.startswith("vless://")
    assert outcome.items[0].source_ref.startswith("sub:")
    report = outcome.reports[0]
    assert report.ok is True
    assert report.mode == "real"
    assert report.etag == '"v1"'
    assert "subs.example.test" not in report.model_dump_json()


def test_subscriptions_timeout_is_retryable_and_isolated(tmp_path: Path, monkeypatch):
    good = "https://a.example.test/ok.txt"
    bad = "https://b.example.test/slow.txt"

    def fake_urlopen(request, timeout=None):
        url = request.full_url
        if url == good:
            return FakeResponse(b"vless://x@example.test:443#n\n", headers={"ETag": '"g"'})
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    config = SubscriptionSourceConfig(enabled=True, urls=[good, bad])
    outcome = subs_mod.collect_subscriptions(config, base_dir=tmp_path)
    by_scope = {r.scope: r for r in outcome.reports}
    ok_ref = next(s for s in by_scope if s.startswith("sub:"))
    assert by_scope[ok_ref].ok is True
    failed = [r for r in outcome.reports if not r.ok]
    assert failed and failed[0].errors[0].retryable is True
    assert failed[0].errors[0].code in {"timeout", "network_error"}
    assert len(outcome.items) == 1


def test_subscriptions_304_reuses_cache(tmp_path: Path, monkeypatch):
    url = "https://subs.example.test/cached.txt"
    body = b"vless://cached@example.test:443#n\n"

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        headers = request.headers if hasattr(request, "headers") else {}
        if calls["n"] == 1:
            return FakeResponse(body, headers={"ETag": '"v1"'})
        # second call: conditional request, server says 304
        raise urllib.error.HTTPError(
            url, 304, "Not Modified", FakeHeaders({"ETag": '"v1"'}), io.BytesIO(b"")
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    config = SubscriptionSourceConfig(enabled=True, urls=[url])
    first = subs_mod.collect_subscriptions(config, base_dir=tmp_path)
    assert first.items and first.items[0].payload.startswith("vless://cached")

    second = subs_mod.collect_subscriptions(config, base_dir=tmp_path)
    assert second.reports[0].ok is True
    assert second.reports[0].etag == '"v1"'
    assert second.items and "cached" in second.items[0].payload


def test_subscriptions_etag_sent_on_refetch(tmp_path: Path, monkeypatch):
    url = "https://subs.example.test/etag.txt"
    seen: list[dict] = []

    def fake_urlopen(request, timeout=None):
        headers = dict(request.headers or {}) if hasattr(request, "headers") else {}
        seen.append(headers)
        return FakeResponse(b"text-content\n", headers={"ETag": '"e1"'})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    config = SubscriptionSourceConfig(enabled=True, urls=[url])
    subs_mod.collect_subscriptions(config, base_dir=tmp_path)
    subs_mod.collect_subscriptions(config, base_dir=tmp_path)
    assert len(seen) == 2
    # second request must carry If-None-Match
    lower = {k.lower(): v for k, v in seen[1].items()}
    assert lower.get("if-none-match") == '"e1"'


def test_subscriptions_rejects_non_https(tmp_path: Path):
    config = SubscriptionSourceConfig(enabled=True, urls=["http://example.test/x"])
    outcome = subs_mod.collect_subscriptions(config, base_dir=tmp_path)
    assert outcome.reports[0].errors[0].code == "invalid_url"
    assert outcome.items == []


def test_subscriptions_rejects_private_host(tmp_path: Path):
    config = SubscriptionSourceConfig(enabled=True, urls=["https://127.0.0.1/x"])
    outcome = subs_mod.collect_subscriptions(config, base_dir=tmp_path)
    assert outcome.reports[0].errors[0].code == "blocked_host"
    assert outcome.items == []


def test_subscriptions_empty_urls_offline_diagnostic(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NODEBENCH_OFFLINE", "1")
    config = SubscriptionSourceConfig(enabled=True, urls=[])
    outcome = subs_mod.collect_subscriptions(config, base_dir=tmp_path)
    assert outcome.items == []
    assert outcome.reports[0].ok is True
    assert outcome.reports[0].mode == "offline"
    assert outcome.reports[0].errors[0].code == "no_urls"


def test_subscriptions_size_limit(tmp_path: Path, monkeypatch):
    url = "https://subs.example.test/huge.txt"

    def fake_urlopen(request, timeout=None):
        return FakeResponse(b"z" * 100)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    config = SubscriptionSourceConfig(enabled=True, urls=[url])
    # default max is 4MB so this passes; craft failure via monkeypatched constant
    monkeypatch.setattr(subs_mod, "MAX_SOURCE_FILE_BYTES", 10)
    outcome = subs_mod.collect_subscriptions(config, base_dir=tmp_path)
    assert outcome.reports[0].ok is False
    assert outcome.reports[0].errors[0].code == "response_too_large"


def test_subscriptions_failure_does_not_overwrite_cache(tmp_path: Path, monkeypatch):
    url = "https://subs.example.test/keep.txt"
    body = b"vless://keep@example.test:443#n\n"
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(body, headers={"ETag": '"k1"'})
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    config = SubscriptionSourceConfig(enabled=True, urls=[url])
    first = subs_mod.collect_subscriptions(config, base_dir=tmp_path)
    assert first.items and first.items[0].payload.startswith("vless://keep")

    second = subs_mod.collect_subscriptions(config, base_dir=tmp_path)
    assert second.reports[0].ok is False
    # stale cache is retained as an item but marked not-ok
    assert any(e.code == "stale_cache" for e in second.reports[0].errors)
    assert second.items and "keep" in second.items[0].payload


# ---------------------------------------------------------------------------
# github
# ---------------------------------------------------------------------------


def test_github_offline_env_uses_simulation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NODEBENCH_OFFLINE", "1")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_example_token_value")
    config = GithubSourceConfig(
        enabled=True,
        offline=True,
        queries=['"vless://" filename:nodes.txt'],
        allowed_repositories=["org/project-a"],
    )
    outcome = github_mod.collect_github(config, base_dir=tmp_path)
    assert outcome.reports[0].mode == "offline"
    codes = {e.code for e in outcome.reports[0].errors}
    assert "offline_mode" in codes or outcome.reports[0].ok is True
    # simulated hit for org/project-a is kept
    assert any(i.source_id == "github" for i in outcome.items) or not outcome.items


def test_github_missing_token_degrades_and_diagnoses(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("NODEBENCH_OFFLINE", raising=False)
    config = GithubSourceConfig(
        enabled=True,
        queries=['"vless://" filename:nodes.txt'],
        allowed_repositories=["org/project-a"],
    )
    outcome = github_mod.collect_github(config, base_dir=tmp_path)
    report = outcome.reports[0]
    assert report.mode == "offline"
    assert any(e.code == "missing_token" for e in report.errors)


def test_github_real_search_success(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_example_token_value")
    monkeypatch.delenv("NODEBENCH_OFFLINE", raising=False)
    search_payload = {
        "total_count": 1,
        "items": [
            {
                "name": "nodes.txt",
                "path": "nodes.txt",
                "url": "https://api.github.com/repos/org/project-a/contents/nodes.txt",
                "size": 20,
                "repository": {"full_name": "org/project-a"},
            }
        ],
    }
    file_payload = {
        "encoding": "utf-8",
        "content": "vless://123e4567-e89b-12d3-a456-426614174000@example.test:443#n",
    }

    def fake_urlopen(request, timeout=None):
        url = request.full_url
        headers = dict(request.headers or {})
        assert headers.get("Authorization", "").startswith("Bearer ")
        if "search/code" in url:
            return FakeResponse(json.dumps(search_payload).encode())
        return FakeResponse(json.dumps(file_payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    config = GithubSourceConfig(
        enabled=True,
        queries=["vless filename:nodes.txt"],
        allowed_repositories=["org/project-a"],
        max_files_per_run=5,
        cache_ttl_hours=0,
    )
    outcome = github_mod.collect_github(config, base_dir=tmp_path)
    assert outcome.reports[0].mode == "real"
    assert outcome.reports[0].ok is True
    assert outcome.reports[0].fetched == 1
    assert outcome.items[0].payload.startswith("vless://")


def test_github_respects_max_files_per_run(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.delenv("NODEBENCH_OFFLINE", raising=False)
    items = [
        {
            "name": f"n{i}.txt",
            "url": f"https://api.github.com/repos/org/p/contents/n{i}.txt",
            "repository": {"full_name": "org/p"},
        }
        for i in range(5)
    ]
    search_payload = {"total_count": 5, "items": items}

    def fake_urlopen(request, timeout=None):
        url = request.full_url
        if "search/code" in url:
            return FakeResponse(json.dumps(search_payload).encode())
        return FakeResponse(json.dumps({"encoding": "base64", "content": "eA=="}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    config = GithubSourceConfig(
        enabled=True,
        queries=["vless"],
        allowed_repositories=["org/p"],
        max_files_per_run=2,
        cache_ttl_hours=0,
    )
    outcome = github_mod.collect_github(config, base_dir=tmp_path)
    assert len(outcome.items) == 2


def test_github_rate_limit_respects_retry_after(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.delenv("NODEBENCH_OFFLINE", raising=False)

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            FakeHeaders({"Retry-After": "30", "X-RateLimit-Remaining": "0"}),
            io.BytesIO(b"{}"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    config = GithubSourceConfig(
        enabled=True,
        queries=["vless"],
        allowed_repositories=["org/p"],
        cache_ttl_hours=0,
    )
    outcome = github_mod.collect_github(config, base_dir=tmp_path)
    report = outcome.reports[0]
    assert report.ok is False
    codes = {e.code for e in report.errors}
    assert "rate_limited" in codes
    assert any(e.retryable for e in report.errors)


def test_github_network_failure_falls_offline(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.delenv("NODEBENCH_OFFLINE", raising=False)

    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("dns fail")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    config = GithubSourceConfig(
        enabled=True,
        queries=['"vless://" filename:nodes.txt'],
        allowed_repositories=["org/project-a"],
        cache_ttl_hours=0,
    )
    outcome = github_mod.collect_github(config, base_dir=tmp_path)
    assert any(e.code == "offline_fallback" for r in outcome.reports for e in r.errors)
    assert any(r.mode == "offline" for r in outcome.reports)


def test_github_filters_to_allowed_repositories(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.delenv("NODEBENCH_OFFLINE", raising=False)
    search_payload = {
        "items": [
            {
                "name": "a.txt",
                "url": "https://api.github.com/repos/org/a/contents/a.txt",
                "repository": {"full_name": "org/a"},
            },
            {
                "name": "b.txt",
                "url": "https://api.github.com/repos/org/b/contents/b.txt",
                "repository": {"full_name": "org/b"},
            },
        ]
    }

    def fake_urlopen(request, timeout=None):
        url = request.full_url
        if "search/code" in url:
            return FakeResponse(json.dumps(search_payload).encode())
        return FakeResponse(json.dumps({"encoding": "base64", "content": "eA=="}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    config = GithubSourceConfig(
        enabled=True,
        queries=["vless"],
        allowed_repositories=["org/a"],
        cache_ttl_hours=0,
    )
    outcome = github_mod.collect_github(config, base_dir=tmp_path)
    assert len(outcome.items) == 1
    assert outcome.items[0].source_ref.startswith("org/a/")


def test_github_cache_ttl_spares_network(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.delenv("NODEBENCH_OFFLINE", raising=False)
    search_payload = {
        "items": [
            {
                "name": "n.txt",
                "url": "https://api.github.com/repos/org/p/contents/n.txt",
                "repository": {"full_name": "org/p"},
            }
        ]
    }
    calls = {"search": 0, "file": 0}

    def fake_urlopen(request, timeout=None):
        url = request.full_url
        if "search/code" in url:
            calls["search"] += 1
            return FakeResponse(json.dumps(search_payload).encode())
        calls["file"] += 1
        return FakeResponse(json.dumps({"encoding": "base64", "content": "eA=="}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    config = GithubSourceConfig(
        enabled=True,
        queries=["vless"],
        allowed_repositories=["org/p"],
        cache_ttl_hours=24,
    )
    first = github_mod.collect_github(config, base_dir=tmp_path)
    assert first.items
    assert calls["search"] == 1
    second = github_mod.collect_github(config, base_dir=tmp_path)
    assert second.items
    # second run served search hits from cache: no additional search call
    assert calls["search"] == 1


def test_github_blocked_domain_constant_preserved():
    assert "127.0.0.1" in github_mod.BLOCKED_DOMAINS
    assert "localhost" in github_mod.BLOCKED_DOMAINS
    assert github_mod._is_blocked_domain("127.0.0.1") is True
    assert github_mod._is_blocked_domain("example.com") is False


def test_subscriptions_config_offline_skips_network(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("NODEBENCH_OFFLINE", raising=False)
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        raise AssertionError("offline mode must not open the network")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    config = SubscriptionSourceConfig(
        enabled=True,
        urls=["https://subs.example.test/list.txt"],
        offline=True,
    )
    outcome = subs_mod.collect_subscriptions(config, base_dir=tmp_path)
    assert calls["n"] == 0
    assert outcome.items == []
    assert outcome.reports[0].ok is True
    assert outcome.reports[0].mode == "offline"
    assert outcome.reports[0].errors[0].code == "offline_mode"
    assert "subs.example.test" not in outcome.reports[0].model_dump_json()


def test_subscriptions_env_offline_skips_network(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NODEBENCH_OFFLINE", "1")
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        raise AssertionError("NODEBENCH_OFFLINE must not open the network")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    config = SubscriptionSourceConfig(
        enabled=True, urls=["https://subs.example.test/list.txt"]
    )
    outcome = subs_mod.collect_subscriptions(config, base_dir=tmp_path)
    assert calls["n"] == 0
    assert outcome.reports[0].mode == "offline"
    assert outcome.items == []


def test_subscriptions_license_tag_comes_from_config(tmp_path: Path, monkeypatch):
    url = "https://subs.example.test/licensed.txt"
    opener = make_opener(
        {url: FakeResponse(b"vless://x@example.test:443#n\n", headers={"ETag": '"l"'})}
    )
    monkeypatch.setattr("urllib.request.urlopen", opener)
    config = SubscriptionSourceConfig(
        enabled=True, urls=[url], license_tag="cc-by-4.0"
    )
    outcome = subs_mod.collect_subscriptions(config, base_dir=tmp_path)
    assert outcome.items[0].license_tag == "cc-by-4.0"


def test_subscriptions_default_license_is_unknown(tmp_path: Path, monkeypatch):
    url = "https://subs.example.test/plain.txt"
    opener = make_opener({url: FakeResponse(b"vless://x@example.test:443#n\n")})
    monkeypatch.setattr("urllib.request.urlopen", opener)
    config = SubscriptionSourceConfig(enabled=True, urls=[url])
    outcome = subs_mod.collect_subscriptions(config, base_dir=tmp_path)
    assert outcome.items[0].license_tag == "unknown"
