"""Cached lookup factories that bridge intelligence services to scoring.

Scoring consumes plain callables; this module owns the concrete
Geo/reputation wiring so ``scoring`` does not import service internals.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable

from nodebench.intelligence.geo import (
    DEFAULT_TIMEOUT_S,
    GeoLookupError,
    GeoResult,
    lookup_geo,
)
from nodebench.intelligence.reputation import make_reputation_provider

MAX_GEO_CACHE = 256
_CF_ASN_MARKERS = ("13335", "cloudflare")


def _is_ip(address: str) -> bool:
    text = str(address or "").strip().strip("[]")
    if not text:
        return False
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _resolve_ip(address: str) -> str | None:
    text = str(address or "").strip()
    if not text:
        return None
    ip = text if _is_ip(text) else None
    if ip is not None:
        return ip
    try:
        infos = socket.getaddrinfo(text, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError:
        return None
    for info in infos:
        candidate = str(info[4][0] or "").strip()
        if candidate and _is_ip(candidate):
            return candidate
    return None


def make_geo_lookup(
    base_url: str = "",
    timeout: float = DEFAULT_TIMEOUT_S,
    *,
    max_cache: int = MAX_GEO_CACHE,
    lookup: Callable[[str, str, float], GeoResult] | None = None,
) -> Callable[[str], GeoResult | None]:
    """Bounded, cached GeoIP resolver used by scoring.

    Only literal IPs are looked up. Failures and unconfigured base URLs
    return ``None`` (unknown) and are cached so a flaky service is not
    hammered. ``lookup`` is injectable for tests.
    """
    root = str(base_url or "").strip()
    resolve = lookup or (lambda ip, url, t: lookup_geo(ip, base_url=url, timeout=t))
    cache: dict[str, GeoResult | None] = {}

    def _lookup(address: str) -> GeoResult | None:
        ip = _resolve_ip(address)
        if ip is None:
            return None
        if ip in cache:
            return cache[ip]
        if not root:
            return None
        if len(cache) >= max_cache:
            cache.pop(next(iter(cache)), None)
        try:
            result = resolve(ip, root, float(timeout))
        except GeoLookupError:
            result = None
        cache[ip] = result
        return result

    return _lookup


def make_risk_lookup(
    *,
    enabled: bool,
    timeout: float = 8.0,
    max_cache: int = 256,
    geo_lookup: Callable[[str], GeoResult | None] | None = None,
) -> Callable[[str], float | None] | None:
    """Bounded, cached AbuseIPDB risk resolver (0-100, higher = riskier).

    Returns ``None`` when reputation is disabled or no API key is configured,
    so scoring can fall back to its neutral default without inventing purity.
    """
    if not enabled:
        return None
    provider = make_reputation_provider(enabled=True, timeout=timeout)
    if getattr(provider, "name", "") == "null":
        return None
    cache: dict[str, float | None] = {}

    def _lookup(address: str) -> float | None:
        ip = _resolve_ip(address)
        if ip is None:
            return None
        if ip in cache:
            return cache[ip]
        if len(cache) >= max_cache:
            cache.pop(next(iter(cache)), None)
        try:
            obs = provider.lookup("risk-lookup", ip)
        except Exception:
            obs = None
        value = None
        if obs is not None and getattr(obs, "risk", None) is not None:
            try:
                value = float(obs.risk)
            except (TypeError, ValueError):
                value = None
        # Refine with IPinfo type signals: anycast/CDN is safer, hosting is riskier.
        if value is not None and geo_lookup is not None:
            g = geo_lookup(ip)
            if g is not None:
                text = f"{getattr(g, 'isp', '')} {getattr(g, 'asn', '')}".lower()
                if getattr(g, "anycast", False) or any(
                    marker in text for marker in _CF_ASN_MARKERS
                ):
                    value = max(0.0, value - 5.0)
                elif any(
                    marker in text
                    for marker in ("hosting", "datacenter", "server", "cloud")
                ):
                    value = min(100.0, value + 5.0)
        cache[ip] = value
        return value

    return _lookup


__all__ = [
    "MAX_GEO_CACHE",
    "make_geo_lookup",
    "make_risk_lookup",
]
