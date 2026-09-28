from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from nodebench.core.config import IntelligenceConfig
from nodebench.core.schema import (
    ProbeMode,
    ProbeStatus,
    ProxyProbeResult,
    Status,
)
from nodebench.intelligence import exit_ip as exit_ip_mod
from nodebench.intelligence import geo as geo_mod
from nodebench.intelligence import reputation as rep_mod
from nodebench.intelligence.reputation import (
    NullProvider,
    SimpleHttpReputationProvider,
    make_reputation_provider,
    normalize_risk,
    risk_level_for,
)
from nodebench.intelligence.service import IntelligenceService

RUN_ID = "20260928T033700Z-abcdef"
RUNNER = "local:desktop-a"


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body if n < 0 else self._body[:n]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def make_probe(
    item_id: str = "node-1",
    *,
    exit_ip: str = "203.0.113.9",
    status: ProbeStatus = ProbeStatus.OK,
) -> ProxyProbeResult:
    return ProxyProbeResult(
        run_id=RUN_ID,
        runner_id=RUNNER,
        status=status,
        probe_mode=ProbeMode.REAL,
        backend="mihomo",
        attempts=1,
        item_id=item_id,
        total_latency_ms=120.0,
        download_bytes=1_048_576,
        speed_mb_s=2.5,
        proxy_exit_ip=exit_ip,
    )


# ---------------------------------------------------------------------------
# exit_ip
# ---------------------------------------------------------------------------


def test_lookup_exit_ip_parses_json(monkeypatch):
    def fake_open(request, timeout=None):
        assert "echo.example" in request.full_url
        return FakeResponse(b'{"ip": "203.0.113.7"}')

    monkeypatch.setattr(exit_ip_mod.urllib.request, "build_opener", lambda *h: type(
        "O", (), {"open": staticmethod(fake_open)}
    )())
    assert exit_ip_mod.lookup_exit_ip("http://127.0.0.1:7890", "http://echo.example/ip") == "203.0.113.7"


def test_lookup_exit_ip_parses_plain_text(monkeypatch):
    monkeypatch.setattr(
        exit_ip_mod.urllib.request,
        "build_opener",
        lambda *h: type("O", (), {"open": staticmethod(lambda r, timeout=None: FakeResponse(b"203.0.113.8"))})(),
    )
    assert exit_ip_mod.lookup_exit_ip("http://127.0.0.1:7890", "http://echo.example") == "203.0.113.8"


def test_lookup_exit_ip_requires_echo_url():
    with pytest.raises(exit_ip_mod.ExitIpError):
        exit_ip_mod.lookup_exit_ip("http://127.0.0.1:7890", "")


def test_lookup_exit_ip_requires_proxy_url():
    with pytest.raises(exit_ip_mod.ExitIpError):
        exit_ip_mod.lookup_exit_ip("", "http://echo.example")


# ---------------------------------------------------------------------------
# geo
# ---------------------------------------------------------------------------


def test_lookup_geo_success(monkeypatch):
    payload = {
        "status": "success",
        "countryCode": "JP",
        "as": "AS15169 Google LLC",
        "isp": "Google LLC",
        "query": "203.0.113.7",
    }

    def fake_urlopen(request, timeout=None):
        assert "203.0.113.7" in request.full_url
        return FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(geo_mod.urllib.request, "urlopen", fake_urlopen)
    result = geo_mod.lookup_geo("203.0.113.7", base_url="http://geo.example/json")
    assert result.country_code == "JP"
    assert result.asn == "AS15169 Google LLC"
    assert result.isp == "Google LLC"


def test_lookup_geo_failure_raises(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(geo_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(geo_mod.GeoLookupError):
        geo_mod.lookup_geo("203.0.113.7", base_url="http://geo.example/json")


def test_lookup_geo_status_fail_raises(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return FakeResponse(json.dumps({"status": "fail", "message": "reserved"}).encode())

    monkeypatch.setattr(geo_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(geo_mod.GeoLookupError):
        geo_mod.lookup_geo("203.0.113.7", base_url="http://geo.example/json")


# ---------------------------------------------------------------------------
# reputation
# ---------------------------------------------------------------------------


def test_normalize_risk_scales_fraction():
    assert normalize_risk(0.5) == pytest.approx(50.0)
    assert normalize_risk(40.0) == pytest.approx(40.0)
    assert normalize_risk(1.0) == pytest.approx(100.0)
    assert normalize_risk(150.0) == pytest.approx(100.0)


def test_risk_level_for():
    assert risk_level_for(None) == "unknown"
    assert risk_level_for(10.0) == "low"
    assert risk_level_for(30.0) == "medium"
    assert risk_level_for(60.0) == "high"
    assert risk_level_for(90.0) == "critical"


def test_null_provider_reports_unknown_without_faking_purity():
    provider = NullProvider()
    snapshot = provider.lookup("node-1", "203.0.113.9")
    assert snapshot.status is Status.UNKNOWN
    assert snapshot.risk is None
    assert snapshot.raw_score is None
    assert snapshot.risk_level == "unknown"
    assert snapshot.provider == "null"


def test_make_reputation_provider_disabled_is_null():
    provider = make_reputation_provider(enabled=False, url="http://rep.example")
    assert isinstance(provider, NullProvider)


def test_simple_http_provider_ok(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return FakeResponse(json.dumps({"score": 25}).encode())

    monkeypatch.setattr(rep_mod.urllib.request, "urlopen", fake_urlopen)
    provider = SimpleHttpReputationProvider("http://rep.example/check")
    snapshot = provider.lookup("node-1", "203.0.113.9")
    assert snapshot.status is Status.OK
    assert snapshot.raw_score == 25.0
    assert snapshot.risk == 25.0
    assert snapshot.risk_level == "medium"


def test_simple_http_provider_failure_is_unknown_not_clean(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("quota")

    monkeypatch.setattr(rep_mod.urllib.request, "urlopen", fake_urlopen)
    provider = SimpleHttpReputationProvider("http://rep.example/check")
    snapshot = provider.lookup("node-1", "203.0.113.9")
    assert snapshot.status is Status.FAILED
    assert snapshot.risk is None
    assert snapshot.risk_level == "unknown"
    assert snapshot.errors


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


def test_service_success_with_geo_and_reputation(monkeypatch):
    def fake_urlopen(request, timeout=None):
        if "geo.example" in request.full_url:
            return FakeResponse(
                json.dumps(
                    {
                        "status": "success",
                        "countryCode": "US",
                        "as": "AS64500",
                        "isp": "Example",
                    }
                ).encode()
            )
        return FakeResponse(json.dumps({"score": 0.2}).encode())

    monkeypatch.setattr(exit_ip_mod.urllib.request, "urlopen", fake_urlopen)

    config = IntelligenceConfig(
        reputation_enabled=True,
        echo_url="http://echo.example/ip",
        geo_url="http://geo.example/json",
        reputation_url="http://rep.example/check",
        timeout=2.0,
    )
    service = IntelligenceService(config)
    report = service.inspect_results(
        [make_probe("node-1"), make_probe("node-2", exit_ip="203.0.113.10")],
        run_id=RUN_ID,
        runner_id=RUNNER,
    )
    assert report.run_id == RUN_ID
    assert report.counts["items"] == 2
    assert report.counts["exit_ok"] == 2
    assert report.counts["reputation_ok"] == 2
    entry = report.entries[0]
    assert entry.status is Status.OK
    assert entry.country_code == "US"
    assert entry.asn == "AS64500"
    assert entry.isp == "Example"
    assert report.reputations[0].risk == pytest.approx(20.0)


def test_service_geo_failure_marks_unknown(monkeypatch):
    def fake_geo_urlopen(request, timeout=None):
        raise urllib.error.URLError("geo down")

    monkeypatch.setattr(geo_mod.urllib.request, "urlopen", fake_geo_urlopen)
    config = IntelligenceConfig(geo_url="http://geo.example/json", timeout=2.0)
    service = IntelligenceService(config)
    report = service.inspect_results(
        [make_probe()], run_id=RUN_ID, runner_id=RUNNER
    )
    entry = report.entries[0]
    assert entry.exit_ip == "203.0.113.9"
    assert entry.country_code == "unknown"
    assert entry.asn == "unknown"
    assert entry.isp == "unknown"
    assert any(err.code == "geo_lookup_failed" for err in entry.errors)


def test_service_reputation_disabled_does_not_fake_purity():
    config = IntelligenceConfig(reputation_enabled=False)
    service = IntelligenceService(config)
    report = service.inspect_results([make_probe()], run_id=RUN_ID, runner_id=RUNNER)
    snapshot = report.reputations[0]
    assert snapshot.status is Status.UNKNOWN
    assert snapshot.risk is None
    assert snapshot.risk_level == "unknown"
    assert snapshot.provider == "null"


def test_service_caches_geo_by_exit_ip(monkeypatch):
    calls: list[str] = []

    def fake_geo_urlopen(request, timeout=None):
        calls.append(request.full_url)
        return FakeResponse(
            json.dumps(
                {"status": "success", "countryCode": "JP", "as": "AS1", "isp": "A"}
            ).encode()
        )

    monkeypatch.setattr(geo_mod.urllib.request, "urlopen", fake_geo_urlopen)
    config = IntelligenceConfig(geo_url="http://geo.example/json")
    clock = {"now": 100.0}
    service = IntelligenceService(config, cache_ttl_s=60.0, clock=lambda: clock["now"])
    results = [
        make_probe("node-1", exit_ip="203.0.113.9"),
        make_probe("node-2", exit_ip="203.0.113.9"),
        make_probe("node-3", exit_ip="203.0.113.9"),
    ]
    service.inspect_results(results, run_id=RUN_ID, runner_id=RUNNER)
    assert len(calls) == 1

    clock["now"] = 200.0
    service.inspect_results(results, run_id=RUN_ID, runner_id=RUNNER)
    assert len(calls) == 2


def test_service_isolates_single_item_failures(monkeypatch):
    def fake_geo_urlopen(request, timeout=None):
        if "203.0.113.66" in request.full_url:
            raise urllib.error.URLError("bad ip")
        return FakeResponse(
            json.dumps(
                {"status": "success", "countryCode": "JP", "as": "AS1", "isp": "A"}
            ).encode()
        )

    monkeypatch.setattr(geo_mod.urllib.request, "urlopen", fake_geo_urlopen)
    config = IntelligenceConfig(geo_url="http://geo.example/json")
    service = IntelligenceService(config)
    report = service.inspect_results(
        [
            make_probe("good", exit_ip="203.0.113.9"),
            make_probe("bad", exit_ip="203.0.113.66"),
        ],
        run_id=RUN_ID,
        runner_id=RUNNER,
    )
    by_id = {item.item_id: item for item in report.entries}
    assert by_id["good"].country_code == "JP"
    assert by_id["bad"].country_code == "unknown"


def test_service_skips_non_ok_results():
    service = IntelligenceService(IntelligenceConfig())
    report = service.inspect_results(
        [make_probe(status=ProbeStatus.FAIL)],
        run_id=RUN_ID,
        runner_id=RUNNER,
    )
    assert report.entries == []
    assert report.counts["items"] == 0


def test_service_uses_probe_exit_ip_when_echo_missing():
    config = IntelligenceConfig()
    service = IntelligenceService(config)
    report = service.inspect_results([make_probe()], run_id=RUN_ID, runner_id=RUNNER)
    assert report.entries[0].exit_ip == "203.0.113.9"
    assert report.entries[0].status is Status.UNKNOWN  # no geo configured
