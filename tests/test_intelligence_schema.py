from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from nodebench.core.schema import (
    ErrorInfo,
    ExitObservation,
    IntelligenceReport,
    ReputationObservation,
    Status,
)

NOW = datetime(2026, 9, 28, 3, 37, 0, tzinfo=timezone.utc)


def make_exit(**overrides) -> ExitObservation:
    payload = {
        "item_id": "hmac-sha256:example",
        "exit_ip": "203.0.113.7",
        "service": "https://echo.example.test/ip",
        "observed_at": NOW,
        "status": Status.OK,
        "country_code": "JP",
        "asn": "AS15169",
        "isp": "Example ISP",
        "errors": [],
    }
    payload.update(overrides)
    return ExitObservation.model_validate(payload)


def make_rep(**overrides) -> ReputationObservation:
    payload = {
        "item_id": "hmac-sha256:example",
        "exit_ip": "203.0.113.7",
        "provider": "simple_http",
        "raw_score": 12.5,
        "risk": 12.5,
        "risk_level": "low",
        "evidence": "{}",
        "observed_at": NOW,
        "status": Status.OK,
        "errors": [],
    }
    payload.update(overrides)
    return ReputationObservation.model_validate(payload)


def test_exit_observation_rejects_extra_fields():
    payload = make_exit().model_dump()
    payload["surprise"] = True
    with pytest.raises(ValidationError):
        ExitObservation.model_validate(payload)


def test_exit_observation_ok_requires_exit_ip():
    with pytest.raises(ValidationError):
        make_exit(exit_ip="", status=Status.OK)


def test_exit_observation_defaults_to_unknown_geo():
    obs = ExitObservation(item_id="item", status=Status.UNKNOWN)
    assert obs.country_code == "unknown"
    assert obs.asn == "unknown"
    assert obs.isp == "unknown"
    assert obs.errors == []


def test_reputation_observation_rejects_extra_fields():
    payload = make_rep().model_dump()
    payload["surprise"] = 1
    with pytest.raises(ValidationError):
        ReputationObservation.model_validate(payload)


def test_reputation_risk_bounds():
    with pytest.raises(ValidationError):
        make_rep(risk=101.0)
    with pytest.raises(ValidationError):
        make_rep(risk=-1.0)


def test_reputation_ok_requires_risk():
    with pytest.raises(ValidationError):
        make_rep(risk=None, status=Status.OK)


def test_reputation_unknown_allows_missing_risk():
    obs = ReputationObservation(
        item_id="item",
        provider="null",
        status=Status.UNKNOWN,
    )
    assert obs.risk is None
    assert obs.risk_level == "unknown"
    assert obs.raw_score is None


def test_intelligence_report_counts_non_negative():
    with pytest.raises(ValidationError):
        IntelligenceReport(
            run_id="20260928T033700Z-abcdef",
            runner_id="local:desktop-a",
            generated_at=NOW,
            counts={"items": -1},
        )


def test_intelligence_report_shape():
    report = IntelligenceReport(
        run_id="20260928T033700Z-abcdef",
        runner_id="local:desktop-a",
        generated_at=NOW,
        counts={"items": 1, "exit_ok": 1},
        entries=[make_exit()],
        reputations=[make_rep()],
    )
    assert report.entries[0].exit_ip == "203.0.113.7"
    assert report.reputations[0].risk == 12.5


def test_error_info_reused():
    error = ErrorInfo(
        stage="inspect",
        code="geo_lookup_failed",
        message_redacted="timeout",
        retryable=True,
    )
    obs = make_exit(status=Status.UNKNOWN, errors=[error])
    assert obs.errors[0].code == "geo_lookup_failed"
    assert obs.errors[0].retryable is True
