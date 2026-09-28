from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEFAULT_GEO_BASE_URL = "http://ip-api.com/json"
IPINFO_BASE = "https://ipinfo.io"
DEFAULT_TIMEOUT_S = 5.0
MAX_BODY_BYTES = 8192
UNKNOWN = "unknown"


class GeoLookupError(RuntimeError):
    """Geo/ASN/ISP lookup failed; fields fall back to ``unknown``."""


@dataclass(frozen=True)
class GeoResult:
    country_code: str = UNKNOWN
    asn: str = UNKNOWN
    isp: str = UNKNOWN
    city: str = UNKNOWN
    region: str = UNKNOWN
    anycast: bool = False
    source: str = UNKNOWN


def _clean(value: object) -> str:
    text = str(value or "").strip()
    return text if text else UNKNOWN


def _ipinfo_token() -> str:
    for name in ("IPINFO_TOKEN", "NODEBENCH_IPINFO_TOKEN"):
        token = str(os.environ.get(name, "") or "").strip()
        if token:
            return token
    return ""


def _parse_ipinfo(payload: object) -> GeoResult:
    if not isinstance(payload, dict):
        raise GeoLookupError("ipinfo response must be a JSON object")
    country = str(payload.get("country") or "").strip()
    if not country:
        raise GeoLookupError("ipinfo payload has no country")
    org = str(payload.get("org") or "").strip()
    # org looks like "AS13335 Cloudflare, Inc."
    asn = UNKNOWN
    isp = org
    if org.startswith("AS") and " " in org:
        asn, _, isp = org.partition(" ")
    return GeoResult(
        country_code=_clean(country),
        asn=_clean(asn),
        isp=_clean(isp),
        city=_clean(payload.get("city")),
        region=_clean(payload.get("region")),
        anycast=bool(payload.get("anycast")),
        source="ipinfo",
    )


def _parse_ipapi(payload: object) -> GeoResult:
    if not isinstance(payload, dict):
        raise GeoLookupError("geo response must be a JSON object")
    status = str(payload.get("status") or "")
    if status and status != "success":
        raise GeoLookupError(f"geo service status={status!r}")
    code = str(payload.get("countryCode") or payload.get("country_code") or "").strip()
    asn = str(payload.get("as") or payload.get("asn") or "").strip()
    isp = str(payload.get("isp") or payload.get("org") or "").strip()
    return GeoResult(
        country_code=_clean(code),
        asn=_clean(asn),
        isp=_clean(isp),
        city=_clean(payload.get("city")),
        region=_clean(payload.get("regionName") or payload.get("region")),
        anycast=False,
        source="ip-api",
    )


def _http_json(url: str, timeout: float, headers: dict[str, str] | None = None):
    request = urllib.request.Request(
        url,
        headers=headers or {"User-Agent": "nodebench/0.1", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            body = response.read(MAX_BODY_BYTES)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as err:
        raise GeoLookupError(f"geo request failed: {type(err).__name__}: {err}") from err
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise GeoLookupError(f"geo response is not valid JSON: {err}") from err


def lookup_geo(
    ip: str,
    base_url: str = "",
    timeout: float = DEFAULT_TIMEOUT_S,
) -> GeoResult:
    """Resolve Geo/ASN/ISP for *ip*.

    Preference: IPinfo (when ``IPINFO_TOKEN`` is set) then the configured
    ip-api.com-style endpoint. Failures raise :class:`GeoLookupError`.
    """
    address = str(ip or "").strip()
    if not address:
        raise GeoLookupError("exit ip is required for geo lookup")

    token = _ipinfo_token()
    if token:
        quoted = urllib.parse.quote(address, safe=":.[]")
        payload = _http_json(
            f"{IPINFO_BASE}/{quoted}/json?token={urllib.parse.quote(token)}",
            timeout,
        )
        return _parse_ipinfo(payload)

    root = str(base_url or DEFAULT_GEO_BASE_URL).strip().rstrip("/")
    if not root:
        raise GeoLookupError("geo base_url is not configured")
    url = (
        f"{root}/{urllib.parse.quote(address, safe=':')}"
        "?fields=status,message,country,countryCode,isp,org,as,query,city,regionName"
    )
    return _parse_ipapi(_http_json(url, timeout))


__all__ = [
    "DEFAULT_GEO_BASE_URL",
    "DEFAULT_TIMEOUT_S",
    "GeoLookupError",
    "GeoResult",
    "lookup_geo",
    "UNKNOWN",
]
