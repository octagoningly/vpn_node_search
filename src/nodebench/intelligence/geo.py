from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEFAULT_GEO_BASE_URL = "http://ip-api.com/json"
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


def _clean(value: object) -> str:
    text = str(value or "").strip()
    return text if text else UNKNOWN


def _parse_payload(payload: object) -> GeoResult:
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
    )


def lookup_geo(
    ip: str,
    base_url: str = DEFAULT_GEO_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> GeoResult:
    """Resolve GeoIP/ASN/ISP for *ip* via an ip-api.com-style JSON endpoint.

    Any failure raises :class:`GeoLookupError`; callers keep ``unknown``
    placeholders instead of guessing.
    """
    address = str(ip or "").strip()
    if not address:
        raise GeoLookupError("exit ip is required for geo lookup")
    root = str(base_url or "").strip().rstrip("/")
    if not root:
        raise GeoLookupError("geo base_url is not configured")
    url = f"{root}/{urllib.parse.quote(address, safe=':')}?fields=status,message,country,countryCode,isp,org,as,query"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "nodebench/0.1", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            body = response.read(MAX_BODY_BYTES)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as err:
        raise GeoLookupError(f"geo request failed: {type(err).__name__}: {err}") from err
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise GeoLookupError(f"geo response is not valid JSON: {err}") from err
    return _parse_payload(payload)


__all__ = [
    "DEFAULT_GEO_BASE_URL",
    "DEFAULT_TIMEOUT_S",
    "GeoLookupError",
    "GeoResult",
    "lookup_geo",
    "UNKNOWN",
]
