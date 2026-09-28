from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from nodebench.core.schema import ErrorInfo, ReputationObservation, Status

DEFAULT_TIMEOUT_S = 5.0
MAX_BODY_BYTES = 8192
SCORE_KEYS = ("score", "risk", "risk_score", "abuse_confidence_score", "raw_score")
RISK_LOW = 25.0
RISK_MEDIUM = 50.0
RISK_HIGH = 75.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def normalize_risk(raw_score: float) -> float:
    """Map a provider raw score onto the 0-100 risk scale (higher = riskier).

    Scores in ``[0, 1]`` are treated as fractions and scaled by 100; scores in
    ``(1, 100]`` are kept as-is. Values outside 0-100 after scaling are clamped.
    """
    number = float(raw_score)
    if 0.0 <= number <= 1.0:
        number *= 100.0
    return max(0.0, min(100.0, number))


def risk_level_for(risk: float | None) -> str:
    if risk is None:
        return "unknown"
    if risk < RISK_LOW:
        return "low"
    if risk < RISK_MEDIUM:
        return "medium"
    if risk < RISK_HIGH:
        return "high"
    return "critical"


class ReputationProvider(ABC):
    """Optional reputation plugin; never invents a clean score on failure."""

    name: str = "abstract"

    @abstractmethod
    def lookup(self, item_id: str, exit_ip: str) -> ReputationObservation:
        """Return a snapshot for *exit_ip*; failures use status != ok."""


class NullProvider(ReputationProvider):
    """Provider used when reputation is disabled; always ``unknown``."""

    name = "null"

    def lookup(self, item_id: str, exit_ip: str) -> ReputationObservation:
        return ReputationObservation(
            item_id=item_id,
            exit_ip=exit_ip or "",
            provider=self.name,
            raw_score=None,
            risk=None,
            risk_level="unknown",
            evidence="",
            observed_at=_utc_now(),
            status=Status.UNKNOWN,
            errors=[],
        )


class SimpleHttpReputationProvider(ReputationProvider):
    """Fetch a JSON score from a configurable HTTP endpoint.

    Expected payload is a JSON object carrying a numeric score under one of
    :data:`SCORE_KEYS`. The raw score is kept verbatim and a normalized
    0-100 ``risk`` is attached.
    """

    name = "simple_http"

    def __init__(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        provider_name: str = "",
    ) -> None:
        self.url = str(url or "").strip()
        self.timeout = float(timeout)
        if provider_name:
            self.name = str(provider_name)

    def _failed(
        self, item_id: str, exit_ip: str, error: ErrorInfo
    ) -> ReputationObservation:
        return ReputationObservation(
            item_id=item_id,
            exit_ip=exit_ip or "",
            provider=self.name,
            raw_score=None,
            risk=None,
            risk_level="unknown",
            evidence="",
            observed_at=_utc_now(),
            status=Status.FAILED,
            errors=[error],
        )

    def lookup(self, item_id: str, exit_ip: str) -> ReputationObservation:
        if not self.url:
            return self._failed(
                item_id,
                exit_ip,
                ErrorInfo(
                    stage="inspect",
                    code="reputation_url_missing",
                    message_redacted="reputation_url is not configured",
                    retryable=False,
                ),
            )
        if not str(exit_ip or "").strip():
            return self._failed(
                item_id,
                exit_ip,
                ErrorInfo(
                    stage="inspect",
                    code="exit_ip_missing",
                    message_redacted="exit ip is required for reputation lookup",
                    retryable=False,
                ),
            )
        query = self.url
        if "{ip}" in query:
            query = query.replace("{ip}", urllib.parse.quote(exit_ip, safe=""))
        elif "ip=" not in query:
            joiner = "&" if "?" in query else "?"
            query = f"{query}{joiner}ip={urllib.parse.quote(exit_ip, safe='')}"
        request = urllib.request.Request(
            query,
            headers={"User-Agent": "nodebench/0.1", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(MAX_BODY_BYTES)
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            OSError,
        ) as err:
            return self._failed(
                item_id,
                exit_ip,
                ErrorInfo(
                    stage="inspect",
                    code="reputation_http_error",
                    message_redacted=f"{type(err).__name__}: {err}",
                    retryable=True,
                ),
            )
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            return self._failed(
                item_id,
                exit_ip,
                ErrorInfo(
                    stage="inspect",
                    code="reputation_parse_error",
                    message_redacted=f"invalid reputation JSON: {err}",
                    retryable=False,
                ),
            )
        raw_score: float | None = None
        if isinstance(payload, dict):
            for key in SCORE_KEYS:
                value = payload.get(key)
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    raw_score = float(value)
                    break
        if raw_score is None:
            return self._failed(
                item_id,
                exit_ip,
                ErrorInfo(
                    stage="inspect",
                    code="reputation_score_missing",
                    message_redacted="reputation payload has no numeric score",
                    retryable=False,
                ),
            )
        risk = normalize_risk(raw_score)
        return ReputationObservation(
            item_id=item_id,
            exit_ip=exit_ip or "",
            provider=self.name,
            raw_score=raw_score,
            risk=risk,
            risk_level=risk_level_for(risk),
            evidence=json.dumps(payload, ensure_ascii=False, sort_keys=True)[:512],
            observed_at=_utc_now(),
            status=Status.OK,
            errors=[],
        )


def make_reputation_provider(
    *,
    enabled: bool,
    url: str = "",
    timeout: float = DEFAULT_TIMEOUT_S,
    provider_name: str = "",
) -> ReputationProvider:
    """Return ``NullProvider`` when disabled, else a reputation provider.

    Preference order:
    1. ``ABUSEIPDB_KEY`` (or ``NODEBENCH_ABUSEIPDB_KEY``) → AbuseIPDB
    2. configured HTTP URL → SimpleHttpReputationProvider
    3. otherwise NullProvider (never invents a clean score)
    """
    if not enabled:
        return NullProvider()
    # Explicit configured URL wins over built-in providers.
    if url:
        return SimpleHttpReputationProvider(
            url, timeout=timeout, provider_name=provider_name
        )
    api_key = ""
    for name in ("ABUSEIPDB_KEY", "NODEBENCH_ABUSEIPDB_KEY"):
        api_key = str(os.environ.get(name, "") or "").strip()
        if api_key:
            break
    if api_key:
        return AbuseIPDBProvider(api_key, timeout=timeout)
    return NullProvider()


class AbuseIPDBProvider(ReputationProvider):
    """AbuseIPDB v2 check endpoint → composite 0-100 risk.

    Primary signal is ``abuseConfidenceScore`` (0-100). The final risk also
    folds in ``isTor``, ``usageType`` and report volume so a quiet-but-shady
    datacenter IP is not scored identical to a whitelisted CDN edge.
    """

    name = "abuseipdb"
    API_URL = "https://api.abuseipdb.com/api/v2/check"

    def __init__(self, api_key: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self.api_key = str(api_key or "").strip()
        self.timeout = float(timeout)

    def _failed(
        self, item_id: str, exit_ip: str, error: ErrorInfo
    ) -> ReputationObservation:
        return ReputationObservation(
            item_id=item_id,
            exit_ip=exit_ip or "",
            provider=self.name,
            raw_score=None,
            risk=None,
            risk_level="unknown",
            evidence="",
            observed_at=_utc_now(),
            status=Status.FAILED,
            errors=[error],
        )

    @staticmethod
    def composite_risk(payload: dict) -> tuple[float, float]:
        """Return (raw_score, risk) on a 0-100 scale (higher = riskier).

        ``abuseConfidenceScore`` is already 0-100. Extra signals adjust it:
        - ``isTor`` forces at least 90
        - hosting / datacenter usage adds a small penalty
        - report volume adds a capped penalty even when confidence is 0
        - explicit whitelist trims a little risk
        """
        confidence = payload.get("abuseConfidenceScore")
        try:
            risk = float(confidence) if confidence is not None else 0.0
        except (TypeError, ValueError):
            risk = 0.0
        risk = max(0.0, min(100.0, risk))

        if bool(payload.get("isTor")):
            risk = max(risk, 90.0)

        usage = str(payload.get("usageType") or "").lower()
        if any(marker in usage for marker in ("data center", "hosting", "transit")):
            risk = min(100.0, risk + 8.0)
        elif any(marker in usage for marker in ("mobile", "fixed line", "isp")):
            risk = max(0.0, risk - 3.0)

        try:
            reports = int(payload.get("totalReports") or 0)
        except (TypeError, ValueError):
            reports = 0
        if reports > 0 and risk < 25.0:
            risk = min(25.0, risk + min(15.0, reports * 0.5))

        if payload.get("isWhitelisted") is True:
            risk = max(0.0, risk - 5.0)

        risk = max(0.0, min(100.0, risk))
        raw = confidence if isinstance(confidence, (int, float)) else risk
        return float(raw), risk

    def lookup(self, item_id: str, exit_ip: str) -> ReputationObservation:
        if not self.api_key:
            return self._failed(
                item_id,
                exit_ip,
                ErrorInfo(
                    stage="inspect",
                    code="reputation_key_missing",
                    message_redacted="AbuseIPDB API key is not configured",
                    retryable=False,
                ),
            )
        if not exit_ip:
            return self._failed(
                item_id,
                exit_ip,
                ErrorInfo(
                    stage="inspect",
                    code="exit_ip_missing",
                    message_redacted="exit ip is required for reputation lookup",
                    retryable=False,
                ),
            )
        query = "{0}?ipAddress={1}&maxAgeInDays=90".format(
            self.API_URL, urllib.parse.quote(exit_ip, safe="")
        )
        request = urllib.request.Request(
            query,
            headers={
                "Key": self.api_key,
                "Accept": "application/json",
                "User-Agent": "nodebench/0.1",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(MAX_BODY_BYTES)
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            OSError,
        ) as err:
            return self._failed(
                item_id,
                exit_ip,
                ErrorInfo(
                    stage="inspect",
                    code="reputation_http_error",
                    message_redacted=f"{type(err).__name__}: {err}",
                    retryable=True,
                ),
            )
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            return self._failed(
                item_id,
                exit_ip,
                ErrorInfo(
                    stage="inspect",
                    code="reputation_parse_error",
                    message_redacted=f"invalid reputation JSON: {err}",
                    retryable=False,
                ),
            )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return self._failed(
                item_id,
                exit_ip,
                ErrorInfo(
                    stage="inspect",
                    code="reputation_score_missing",
                    message_redacted="AbuseIPDB payload has no data object",
                    retryable=False,
                ),
            )
        raw_score, risk = self.composite_risk(data)
        evidence = {
            "abuseConfidenceScore": data.get("abuseConfidenceScore"),
            "usageType": data.get("usageType"),
            "isTor": data.get("isTor"),
            "isWhitelisted": data.get("isWhitelisted"),
            "totalReports": data.get("totalReports"),
            "isp": data.get("isp"),
            "countryCode": data.get("countryCode"),
        }
        return ReputationObservation(
            item_id=item_id,
            exit_ip=exit_ip or "",
            provider=self.name,
            raw_score=raw_score,
            risk=risk,
            risk_level=risk_level_for(risk),
            evidence=json.dumps(evidence, ensure_ascii=False, sort_keys=True)[:512],
            observed_at=_utc_now(),
            status=Status.OK,
            errors=[],
        )


__all__ = [
    "AbuseIPDBProvider",
    "DEFAULT_TIMEOUT_S",
    "SCORE_KEYS",
    "NullProvider",
    "ReputationProvider",
    "SimpleHttpReputationProvider",
    "make_reputation_provider",
    "normalize_risk",
    "risk_level_for",
]
