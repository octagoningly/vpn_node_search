from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from nodebench.core.config import IntelligenceConfig
from nodebench.core.schema import (
    ErrorInfo,
    ExitObservation,
    IntelligenceReport,
    ProbeStatus,
    ProxyProbeResult,
    ReputationObservation,
    Status,
)
from nodebench.intelligence.exit_ip import ExitIpError, lookup_exit_ip
from nodebench.intelligence.geo import GeoLookupError, GeoResult, lookup_geo
from nodebench.intelligence.reputation import (
    ReputationProvider,
    make_reputation_provider,
)

DEFAULT_CACHE_TTL_S = 300.0
INTEL_STAGE = "inspect"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _error(code: str, message: str, retryable: bool = False) -> ErrorInfo:
    return ErrorInfo(
        stage=INTEL_STAGE,
        code=code,
        message_redacted=message,
        retryable=retryable,
    )


@dataclass
class _GeoCacheEntry:
    result: GeoResult
    expires_at: float


class IntelligenceService:
    """Facade: exit IP + Geo/ASN/ISP + optional reputation for probed nodes.

    Single-point failures are isolated: one bad lookup marks that observation
    ``unknown``/``failed`` and never aborts the batch. Geo and reputation
    results are cached per ``exit_ip`` for a short TTL to bound outbound
    requests.
    """

    def __init__(
        self,
        config: IntelligenceConfig | None = None,
        *,
        reputation: ReputationProvider | None = None,
        cache_ttl_s: float = DEFAULT_CACHE_TTL_S,
        proxy_url: str = "",
        clock: Any = time.monotonic,
    ) -> None:
        self.config = config or IntelligenceConfig()
        self.proxy_url = str(proxy_url or "").strip()
        self.cache_ttl_s = float(cache_ttl_s)
        self._clock = clock
        self._geo_cache: dict[str, _GeoCacheEntry] = {}
        self._rep_cache: dict[str, tuple[float, ReputationObservation]] = {}
        if reputation is not None:
            self.reputation = reputation
        else:
            self.reputation = make_reputation_provider(
                enabled=bool(self.config.reputation_enabled),
                url=str(self.config.reputation_url or ""),
                timeout=float(self.config.timeout),
            )

    def _cache_fresh(self, expires_at: float) -> bool:
        return float(self._clock()) < expires_at

    def _resolve_exit_ip(self, result: ProxyProbeResult) -> tuple[str, list[ErrorInfo]]:
        errors: list[ErrorInfo] = []
        existing = str(result.proxy_exit_ip or "").strip()
        if existing:
            return existing, errors
        echo_url = str(self.config.echo_url or "").strip()
        proxy_url = self.proxy_url or str(result.measured_via or "").strip()
        if not echo_url:
            errors.append(
                _error("echo_url_missing", "echo_url is not configured", False)
            )
            return "", errors
        if not proxy_url:
            errors.append(
                _error("proxy_url_missing", "local proxy url is unknown", False)
            )
            return "", errors
        try:
            ip = lookup_exit_ip(
                proxy_url,
                echo_url,
                timeout=float(self.config.timeout),
            )
            return ip, errors
        except ExitIpError as err:
            errors.append(_error("exit_ip_lookup_failed", str(err), True))
            return "", errors

    def _geo_for(self, exit_ip: str) -> tuple[GeoResult, list[ErrorInfo]]:
        if not exit_ip:
            return GeoResult(), [
                _error("geo_skipped", "no exit ip for geo lookup", False)
            ]
        now = float(self._clock())
        cached = self._geo_cache.get(exit_ip)
        if cached is not None and self._cache_fresh(cached.expires_at):
            return cached.result, []
        base_url = str(self.config.geo_url or "").strip()
        if not base_url:
            return GeoResult(), [
                _error("geo_url_missing", "geo_url is not configured", False)
            ]
        try:
            result = lookup_geo(
                exit_ip,
                base_url=base_url,
                timeout=float(self.config.timeout),
            )
        except GeoLookupError as err:
            return GeoResult(), [_error("geo_lookup_failed", str(err), True)]
        self._geo_cache[exit_ip] = _GeoCacheEntry(
            result=result, expires_at=now + self.cache_ttl_s
        )
        return result, []

    def _reputation_for(
        self, item_id: str, exit_ip: str
    ) -> tuple[ReputationObservation, list[ErrorInfo]]:
        now = float(self._clock())
        if exit_ip:
            cached = self._rep_cache.get(exit_ip)
            if cached is not None and self._cache_fresh(cached[0]):
                snapshot, _ = cached
                copied = snapshot.model_copy(update={"item_id": item_id})
                return copied, []
        try:
            snapshot = self.reputation.lookup(item_id, exit_ip)
        except Exception as err:  # noqa: BLE001 - isolate provider crashes
            failed = ReputationObservation(
                item_id=item_id,
                exit_ip=exit_ip or "",
                provider=getattr(self.reputation, "name", "unknown"),
                status=Status.FAILED,
                errors=[_error("reputation_error", f"{type(err).__name__}: {err}", True)],
            )
            return failed, []
        if exit_ip and snapshot.status is Status.OK:
            self._rep_cache[exit_ip] = (
                now + self.cache_ttl_s,
                snapshot.model_copy(update={"item_id": item_id}),
            )
        return snapshot, []

    def inspect_results(
        self,
        probe_results: Sequence[ProxyProbeResult],
        *,
        run_id: str,
        runner_id: str,
    ) -> IntelligenceReport:
        """Build an :class:`IntelligenceReport` from status=ok probe results."""
        entries: list[ExitObservation] = []
        reputations: list[ReputationObservation] = []
        ok_items = 0
        unknown_geo = 0
        unknown_rep = 0
        for result in probe_results:
            if getattr(result, "kind", "") != "proxy_probe_result":
                continue
            if result.status is not ProbeStatus.OK:
                continue
            ok_items += 1
            item_id = str(result.item_id)
            exit_ip, exit_errors = self._resolve_exit_ip(result)
            geo, geo_errors = self._geo_for(exit_ip)
            exit_errors = [*exit_errors, *geo_errors]
            if exit_ip:
                status = Status.OK if not geo_errors else Status.UNKNOWN
            else:
                status = Status.UNKNOWN
                unknown_geo += 1
            entry = ExitObservation(
                item_id=item_id,
                exit_ip=exit_ip,
                service=str(self.config.echo_url or ""),
                observed_at=_utc_now(),
                status=status,
                country_code=geo.country_code,
                asn=geo.asn,
                isp=geo.isp,
                errors=exit_errors,
            )
            entries.append(entry)
            if not exit_ip and not str(result.proxy_exit_ip or "").strip():
                unknown_geo += 0  # already counted when no ip
            snapshot, rep_errors = self._reputation_for(item_id, exit_ip)
            if rep_errors and snapshot.status is Status.UNKNOWN:
                snapshot = snapshot.model_copy(
                    update={"errors": [*snapshot.errors, *rep_errors]}
                )
            if snapshot.status is not Status.OK:
                unknown_rep += 1
            reputations.append(snapshot)
        counts = {
            "items": ok_items,
            "exit_ok": sum(1 for item in entries if item.status is Status.OK),
            "exit_unknown": sum(1 for item in entries if item.status is not Status.OK),
            "geo_unknown": unknown_geo,
            "reputation_ok": sum(
                1 for item in reputations if item.status is Status.OK
            ),
            "reputation_unknown": unknown_rep,
        }
        return IntelligenceReport(
            run_id=run_id,
            runner_id=runner_id,
            generated_at=_utc_now(),
            counts=counts,
            entries=entries,
            reputations=reputations,
        )


__all__ = [
    "DEFAULT_CACHE_TTL_S",
    "IntelligenceService",
]
