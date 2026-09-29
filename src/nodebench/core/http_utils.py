from __future__ import annotations

import ipaddress
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

DEFAULT_TIMEOUT_S = 15.0
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_USER_AGENT = "NodeBench/0.1 (+https://github.com/local/nodebench; bounded-research)"
MAX_REDIRECTS = 5
OFFLINE_ENV = "NODEBENCH_OFFLINE"

BLOCKED_HOST_PREFIXES = (
    "127.",
    "localhost",
    "10.",
    "172.16.",
    "172.31.",
    "192.168.",
    "169.254.",
    "0.",
    "::1",
    "fc00:",
    "fe80:",
)

_PRIVATE_NETWORKS = [
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
]


class HttpError(Exception):
    """Structured HTTP / network failure with a retryable flag."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        status: int = 0,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status
        self.headers = dict(headers or {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "status": self.status,
        }


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str
    not_modified: bool = False
    rate_limit_remaining: int | None = None
    retry_after: float | None = None

    @property
    def etag(self) -> str:
        return self.headers.get("etag", self.headers.get("ETag", ""))

    @property
    def last_modified(self) -> str:
        return self.headers.get("last-modified", self.headers.get("Last-Modified", ""))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def offline_requested(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    raw = str(source.get(OFFLINE_ENV, "")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def is_private_host(host: str) -> bool:
    """Return True when *host* is a private / reserved / local name or address."""
    name = (host or "").strip().lower().strip("[]")
    if not name:
        return True
    for prefix in BLOCKED_HOST_PREFIXES:
        if name.startswith(prefix) or name == prefix.rstrip("."):
            return True
    if name in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return True
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        return False
    return any(addr in net for net in _PRIVATE_NETWORKS)


def _normalize_headers(raw: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if not raw:
        return result
    items = raw.items() if hasattr(raw, "items") else []
    for key, value in items:
        result[str(key).lower()] = str(value)
    # keep canonical lookup helpers
    if "etag" in result:
        result["ETag"] = result["etag"]
    if "last-modified" in result:
        result["Last-Modified"] = result["last-modified"]
    return result


def _parse_retry_after(headers: Mapping[str, str]) -> float | None:
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except ValueError:
        return None


def _parse_rate_limit_remaining(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("x-ratelimit-remaining") or headers.get("X-RateLimit-Remaining")
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def scrub_url(text: str) -> str:
    """Remove URL-like substrings so reports never echo target addresses."""
    import re

    return re.sub(r"https?://[^\s\"'<>]+", "[redacted-url]", str(text or ""))


_SYSTEM_OPENER: Any = None


def system_proxies() -> dict[str, str]:
    """Windows system proxy (IE/WinINET registry) + env proxies."""
    proxies = dict(urllib.request.getproxies())
    if os.name != "nt":
        return proxies
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0,
            winreg.KEY_READ,
        )
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        except OSError:
            return proxies
        finally:
            winreg.CloseKey(key)
        if not enable or not server:
            return proxies
        server = str(server).strip()
        if "=" in server and ";" in server:
            for part in server.split(";"):
                if "=" in part:
                    k, _, v = part.partition("=")
                    proxies[k.strip().lower()] = f"http://{v.strip()}"
        else:
            proxies.setdefault("http", f"http://{server}")
            proxies.setdefault("https", f"http://{server}")
    except Exception:
        pass
    return proxies


def install_system_proxy() -> None:
    """Make urllib.request.urlopen honour system proxy (Clash etc.)."""
    global _SYSTEM_OPENER
    proxies = system_proxies()
    if not proxies:
        return
    if _SYSTEM_OPENER is None:
        _SYSTEM_OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler(proxies)
        )
        urllib.request.install_opener(_SYSTEM_OPENER)


# Honour Clash / system proxy at import time so TLS works behind local proxies.
install_system_proxy()


def validate_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https":
        raise HttpError("insecure_scheme", f"only https URLs are allowed: {url!r}", retryable=False)
    host = parsed.hostname or ""
    if not host:
        raise HttpError("invalid_url", f"URL has no host: {url!r}", retryable=False)
    if is_private_host(host):
        raise HttpError("blocked_host", f"host is not allowed: {host!r}", retryable=False)
    return parsed


def _build_request(
    url: str,
    headers: Mapping[str, str] | None,
    user_agent: str,
) -> urllib.request.Request:
    merged: dict[str, str] = {
        "User-Agent": user_agent or DEFAULT_USER_AGENT,
        "Accept": "*/*",
    }
    if headers:
        for key, value in headers.items():
            if value is not None:
                merged[str(key)] = str(value)
    return urllib.request.Request(url, headers=merged, method="GET")


def controlled_get(
    url: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
    user_agent: str = DEFAULT_USER_AGENT,
    headers: Mapping[str, str] | None = None,
    max_redirects: int = MAX_REDIRECTS,
    opener: Any = None,
) -> HttpResponse:
    """HTTPS GET with SSRF checks, size cap, and no private-address redirects."""
    validate_url(url)
    current = url
    request_headers = dict(headers or {})
    for hop in range(max_redirects + 1):
        validate_url(current)
        request = _build_request(current, request_headers, user_agent)
        open_fn = opener if opener is not None else urllib.request.urlopen
        try:
            with open_fn(request, timeout=timeout_s) as response:
                status = int(getattr(response, "status", 0) or response.getcode() or 0)
                raw_headers = getattr(response, "headers", response.info())
                resp_headers = _normalize_headers(raw_headers)
                if status in (301, 302, 303, 307, 308):
                    location = resp_headers.get("location") or resp_headers.get("Location")
                    if not location:
                        raise HttpError(
                            "redirect_missing_location",
                            "redirect response without Location header",
                            retryable=False,
                            status=status,
                            headers=resp_headers,
                        )
                    current = urllib.parse.urljoin(current, location)
                    validate_url(current)
                    continue
                if status == 304:
                    return HttpResponse(
                        status=304,
                        headers=resp_headers,
                        body=b"",
                        url=current,
                        not_modified=True,
                        rate_limit_remaining=_parse_rate_limit_remaining(resp_headers),
                        retry_after=_parse_retry_after(resp_headers),
                    )
                if status >= 400:
                    retry_after = _parse_retry_after(resp_headers)
                    retryable = status in (408, 425, 429, 500, 502, 503, 504) or retry_after is not None
                    raise HttpError(
                        "http_error",
                        f"HTTP {status} for {scrub_url(current)}",
                        retryable=retryable,
                        status=status,
                        headers=resp_headers,
                    )
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise HttpError(
                            "response_too_large",
                            f"response exceeds {max_bytes} bytes",
                            retryable=False,
                            status=status,
                            headers=resp_headers,
                        )
                    chunks.append(chunk)
                return HttpResponse(
                    status=status,
                    headers=resp_headers,
                    body=b"".join(chunks),
                    url=current,
                    rate_limit_remaining=_parse_rate_limit_remaining(resp_headers),
                    retry_after=_parse_retry_after(resp_headers),
                )
        except HttpError:
            raise
        except urllib.error.HTTPError as err:
            resp_headers = _normalize_headers(err.headers)
            if err.code == 304:
                return HttpResponse(
                    status=304,
                    headers=resp_headers,
                    body=b"",
                    url=current,
                    not_modified=True,
                    rate_limit_remaining=_parse_rate_limit_remaining(resp_headers),
                    retry_after=_parse_retry_after(resp_headers),
                )
            retry_after = _parse_retry_after(resp_headers)
            retryable = err.code in (408, 425, 429, 500, 502, 503, 504) or retry_after is not None
            raise HttpError(
                "http_error",
                f"HTTP {err.code} for {scrub_url(current)}",
                retryable=retryable,
                status=int(err.code),
                headers=resp_headers,
            ) from err
        except urllib.error.URLError as err:
            reason = getattr(err, "reason", err)
            if isinstance(reason, socket.timeout):
                raise HttpError("timeout", f"request timed out: {scrub_url(current)}", retryable=True) from err
            raise HttpError(
                "network_error",
                f"network error for {scrub_url(current)}: {scrub_url(str(reason))}",
                retryable=True,
            ) from err
        except TimeoutError as err:
            raise HttpError("timeout", f"request timed out: {scrub_url(current)}", retryable=True) from err
        except OSError as err:
            raise HttpError(
                "network_error",
                f"network error for {scrub_url(current)}: {scrub_url(str(err))}",
                retryable=True,
            ) from err
    raise HttpError(
        "too_many_redirects",
        f"redirect limit {max_redirects} exceeded",
        retryable=False,
    )


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_USER_AGENT",
    "MAX_REDIRECTS",
    "OFFLINE_ENV",
    "HttpError",
    "HttpResponse",
    "controlled_get",
    "offline_requested",
    "is_private_host",
    "validate_url",
    "scrub_url",
]
