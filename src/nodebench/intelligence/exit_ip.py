from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_ECHO_URL = ""
DEFAULT_TIMEOUT_S = 5.0
MAX_BODY_BYTES = 8192
IP_KEYS = ("ip", "origin", "query", "address")


class ExitIpError(RuntimeError):
    """Echo lookup failed; caller records status=unknown."""


def _parse_ip(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        raise ExitIpError("echo response body is empty")
    if text.startswith("{") or text.startswith("["):
        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError as err:
            raise ExitIpError(f"echo response is not valid JSON: {err}") from err
        if isinstance(payload, dict):
            for key in IP_KEYS:
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            raise ExitIpError("echo JSON has no ip field")
        if isinstance(payload, list) and payload and isinstance(payload[0], str):
            return payload[0].strip()
        raise ExitIpError("echo JSON does not contain an IP")
    candidate = text.splitlines()[0].strip()
    if not candidate:
        raise ExitIpError("echo response has no IP text")
    return candidate


def lookup_exit_ip(
    proxy_url: str,
    echo_url: str = DEFAULT_ECHO_URL,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> str:
    """Query *echo_url* through the local HTTP proxy at *proxy_url*.

    The returned IP is the egress address of that proxy path. Raises
    :class:`ExitIpError` on any transport or payload failure so callers can
    mark the observation ``unknown`` instead of inventing an address.
    """
    if not str(proxy_url).strip():
        raise ExitIpError("proxy_url is required to query the echo service")
    if not str(echo_url).strip():
        raise ExitIpError("echo_url is not configured")
    handlers: list[Any] = [
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    ]
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        echo_url,
        headers={"User-Agent": "nodebench/0.1", "Accept": "application/json, text/plain"},
        method="GET",
    )
    try:
        with opener.open(request, timeout=float(timeout)) as response:
            body = response.read(MAX_BODY_BYTES)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as err:
        raise ExitIpError(f"echo request failed: {type(err).__name__}: {err}") from err
    return _parse_ip(body)


__all__ = [
    "DEFAULT_ECHO_URL",
    "DEFAULT_TIMEOUT_S",
    "ExitIpError",
    "lookup_exit_ip",
]
