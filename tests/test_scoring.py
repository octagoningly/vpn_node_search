from __future__ import annotations

import pytest

from nodebench.core.config import (
    ScoringCfWeights,
    ScoringConfig,
    ScoringDiversityConfig,
    ScoringFilters,
    ScoringWeights,
)
from nodebench.core.schema import (
    EdgeEndpoint,
    EndpointProbeResult,
    FailureStage,
    HistorySummary,
    ProbeMode,
    ProbeStatus,
    ProxyNode,
    ProxyProbeResult,
    ScoreReport,
)
from nodebench.intelligence.geo import GeoResult
from nodebench.intelligence.lookups import make_geo_lookup
from nodebench.scoring import (
    SCORING_VERSION,
    latency_score,
    loss_score,
    purity_score,
    score_run,
    speed_score,
    stability_score,
    weighted_score,
)
from nodebench.scoring.rules import CF_ANYCAST_NEUTRAL_RISK

RUN_ID = "20260101T000000Z-abcdef"
RUNNER = "local:desktop-a"
WINDOW = 14


def make_node(item_id: str = "p1", **params: object) -> ProxyNode:
    return ProxyNode(
        item_id=item_id,
        kind="proxy_node",
        fingerprint=f"fp-{item_id}",
        fingerprint_version=1,
        protocol="vless",
        server="1.2.3.4",
        port=443,
        transport="tcp",
        security="tls",
        params=dict(params),
        source_ids=["local:sample-uri-list.txt"],
    )


def make_ok_result(
    item_id: str = "p1",
    latency: float = 100.0,
    speed: float | None = 5.0,
    attempts: int = 4,
    timeouts: int = 0,
    **kwargs: object,
) -> ProxyProbeResult:
    payload: dict[str, object] = {}
    if speed is None:
        payload["download_bytes"] = 0
    else:
        payload["download_bytes"] = 1_000_000
        payload["speed_mb_s"] = speed
    return ProxyProbeResult(
        run_id=RUN_ID,
        runner_id=RUNNER,
        status=ProbeStatus.OK,
        probe_mode=ProbeMode.REAL,
        backend="mihomo",
        item_id=item_id,
        total_latency_ms=latency,
        attempts=attempts,
        timeouts=timeouts,
        **payload,
        **kwargs,
    )


def make_edge(item_id: str = "e1", **params: object) -> EdgeEndpoint:
    return EdgeEndpoint(
        item_id=item_id,
        kind="edge_endpoint",
        fingerprint=f"fp-{item_id}",
        fingerprint_version=1,
        address="1.1.1.1",
        port=443,
        target_host="speed.cloudflare.com",
        tls=True,
        params=dict(params),
        source_ids=["cf:candidates"],
    )


def make_eok(
    item_id: str = "e1",
    latency: float = 200.0,
    speed: float | None = 3.0,
    host_compatible: bool | None = True,
    loss: float | None = 0.0,
    **kwargs: object,
) -> EndpointProbeResult:
    payload: dict[str, object] = {}
    if speed is None:
        payload["speed_mb_s"] = None
    else:
        payload["speed_mb_s"] = speed
    return EndpointProbeResult(
        run_id=RUN_ID,
        runner_id=RUNNER,
        status=ProbeStatus.OK,
        probe_mode=ProbeMode.REAL,
        backend="cfst",
        item_id=item_id,
        address="1.1.1.1",
        port=443,
        latency_ms=latency,
        loss_pct=loss,
        host_compatible=host_compatible,
        **payload,
        **kwargs,
    )


def run_score(
    nodes: tuple[ProxyNode, ...] = (),
    edges: tuple[EdgeEndpoint, ...] = (),
    results: tuple[object, ...] = (),
    proxy_history: dict | None = None,
    endpoint_history: dict | None = None,
    scoring: ScoringConfig | None = None,
    history_days: int = WINDOW,
    geo_lookup=None,
) -> ScoreReport:
    return score_run(
        run_id=RUN_ID,
        runner_id=RUNNER,
        profile="local",
        nodes=list(nodes),
        edges=list(edges),
        probe_results=list(results),
        proxy_history=proxy_history,
        endpoint_history=endpoint_history,
        scoring=scoring or ScoringConfig(),
        history_days=history_days,
        geo_lookup=geo_lookup,
    )


def history(
    item_id: str, samples: int, availability: float | None
) -> dict[str, dict[int, HistorySummary]]:
    if samples == 0:
        return {}
    return {
        item_id: {
            WINDOW: HistorySummary(
                item_id=item_id,
                window_days=WINDOW,
                scheduled=samples + 1,
                executed=samples + 1,
                succeeded=samples,
                sample_count=samples,
                availability_rate=availability,
            )
        }
    }



class TestRules:
    def test_latency_score_bounds(self) -> None:
        assert latency_score(0.0, 800.0) == 1.0
        assert latency_score(800.0, 800.0) == 0.0
        assert latency_score(900.0, 800.0) == 0.0
        with pytest.raises(ValueError):
            latency_score(-1.0, 800.0)
        with pytest.raises(ValueError):
            latency_score(100.0, 0.0)

    def test_speed_score_monotone_and_bounded(self) -> None:
        assert speed_score(0.0) == 0.0
        low = speed_score(1.0)
        high = speed_score(50.0)
        assert 0.0 < low < high < 1.0
        with pytest.raises(ValueError):
            speed_score(-1.0)

    def test_purity_loss_stability(self) -> None:
        assert purity_score(0.0) == 1.0
        assert purity_score(50.0) == 0.5
        assert purity_score(100.0) == 0.0
        assert loss_score(0.0) == 1.0
        assert loss_score(25.0) == 0.75
        assert stability_score(0.4) == 0.4
        with pytest.raises(ValueError):
            purity_score(101.0)
        with pytest.raises(ValueError):
            loss_score(-0.1)
        with pytest.raises(ValueError):
            stability_score(1.5)

    def test_weighted_score_renormalizes_missing_dims(self) -> None:
        weights = {"latency": 0.5, "speed": 0.5}
        score, breakdown = weighted_score(weights, {"latency": 1.0, "speed": None})
        assert score == 1.0
        assert breakdown == {"latency": 1.0}
        score, breakdown = weighted_score(
            weights, {"latency": 1.0, "speed": 0.0}
        )
        assert score == pytest.approx(0.5)
        score, breakdown = weighted_score(weights, {"latency": None, "speed": None})
        assert score == 0.0
        assert breakdown == {}


class TestProxyScoring:
    def test_ok_probe_is_ranked(self) -> None:
        report = run_score(
            nodes=(make_node("p1", risk=10.0),),
            results=(make_ok_result(),),
        )
        assert report.counts["ranked"] == 1
        item = report.proxies[0]
        assert item.status == "ranked"
        assert item.rank == 1
        assert 0.0 < item.score <= 1.0
        assert set(item.score_breakdown) == {"latency", "speed", "loss", "purity"}
        assert item.score_breakdown["purity"] == pytest.approx(0.9)
        assert item.probe_status == "ok"
        assert item.probe_mode == "real"
        assert item.observed_at is not None
        assert item.scoring_version == SCORING_VERSION
        assert item.rule_snapshot["scoring_version"] == SCORING_VERSION
        assert item.notes["stability_samples_insufficient"] is True
        assert item.notes["stability_penalty"] == pytest.approx(0.9)

    def test_purity_missing_is_pending_or_exclude(self) -> None:
        report = run_score(
            nodes=(make_node("p1"),),
            results=(make_ok_result("p1"),),
        )
        item = report.proxies[0]
        assert item.status == "pending"
        assert "purity" in item.pending
        assert item.filters_failed == []

        exclude = ScoringConfig(filters=ScoringFilters(missing="exclude"))
        report = run_score(
            nodes=(make_node("p1"),),
            results=(make_ok_result("p1"),),
            scoring=exclude,
        )
        item = report.proxies[0]
        assert item.status == "filtered"
        assert item.filters_failed == ["missing_purity"]

    def test_missing_probe_pending_then_exclude(self) -> None:
        report = run_score(nodes=(make_node(),))
        item = report.proxies[0]
        assert item.status == "pending"
        assert item.pending == ["probe"]
        assert item.filters_failed == []
        assert item.rank == 0

        exclude = ScoringConfig(filters=ScoringFilters(missing="exclude"))
        report = run_score(nodes=(make_node(),), scoring=exclude)
        item = report.proxies[0]
        assert item.status == "filtered"
        assert item.filters_failed == ["missing_probe"]
        assert item.pending == []

    def test_skipped_probe_is_pending(self) -> None:
        skipped = ProxyProbeResult(
            run_id=RUN_ID,
            runner_id=RUNNER,
            status=ProbeStatus.SKIPPED,
            probe_mode=ProbeMode.REAL,
            backend="mihomo",
            item_id="p1",
            skipped_reason="missing_binary",
        )
        report = run_score(nodes=(make_node(),), results=(skipped,))
        assert report.proxies[0].status == "pending"
        assert report.proxies[0].pending == ["probe"]
        skipped.probe_mode = ProbeMode.NOT_RUN
        report = run_score(nodes=(make_node(),), results=(skipped,))
        assert report.proxies[0].status == "pending"
        assert report.proxies[0].pending == ["probe"]

    def test_measured_failure_is_filtered_probe(self) -> None:
        failed = ProxyProbeResult(
            run_id=RUN_ID,
            runner_id=RUNNER,
            status=ProbeStatus.FAIL,
            failure_stage=FailureStage.TLS,
            probe_mode=ProbeMode.REAL,
            backend="mihomo",
            item_id="p1",
            attempts=1,
        )
        report = run_score(nodes=(make_node(),), results=(failed,))
        item = report.proxies[0]
        assert item.status == "filtered"
        assert item.filters_failed == ["probe"]
        assert item.probe_status == "fail"


    def test_simulated_filtered_when_real_required(self) -> None:
        simulated = make_ok_result()
        simulated.probe_mode = ProbeMode.SIMULATED
        report = run_score(
            nodes=(make_node("p1", risk=10.0),), results=(simulated,)
        )
        item = report.proxies[0]
        assert item.status == "filtered"
        assert item.filters_failed == ["probe_mode"]

        relaxed = ScoringConfig(
            filters=ScoringFilters(require_real_probe_success=False)
        )
        report = run_score(
            nodes=(make_node("p1", risk=10.0),),
            results=(simulated,),
            scoring=relaxed,
        )
        assert report.proxies[0].status == "ranked"

    def test_not_run_mode_filtered_when_real_required(self) -> None:
        not_run = make_ok_result()
        not_run.probe_mode = ProbeMode.NOT_RUN
        report = run_score(
            nodes=(make_node("p1", risk=10.0),), results=(not_run,)
        )
        item = report.proxies[0]
        assert item.status == "filtered"
        assert item.filters_failed == ["probe_mode"]

        relaxed = ScoringConfig(
            filters=ScoringFilters(require_real_probe_success=False)
        )
        report = run_score(
            nodes=(make_node("p1", risk=10.0),),
            results=(not_run,),
            scoring=relaxed,
        )
        assert report.proxies[0].status == "ranked"

    def test_country_filter(self) -> None:
        scoring = ScoringConfig(filters=ScoringFilters(allowed_countries=["US"]))
        report = run_score(
            nodes=(make_node("p1", country="DE", risk=10.0),),
            results=(make_ok_result("p1"),),
            scoring=scoring,
        )
        assert report.proxies[0].filters_failed == ["country"]
        report = run_score(
            nodes=(make_node("p1", country="US", risk=10.0),),
            results=(make_ok_result("p1"),),
            scoring=scoring,
        )
        assert report.proxies[0].status == "ranked"
        report = run_score(
            nodes=(make_node("p1", risk=10.0),),
            results=(make_ok_result("p1"),),
            scoring=scoring,
        )
        assert report.proxies[0].filters_failed == ["country"]

    def test_risk_filter_and_purity_dimension(self) -> None:
        report = run_score(
            nodes=(make_node("p1", risk=60),),
            results=(make_ok_result("p1"),),
        )
        assert report.proxies[0].filters_failed == ["risk"]
        report = run_score(
            nodes=(make_node("p1", risk=10),),
            results=(make_ok_result("p1"),),
        )
        item = report.proxies[0]
        assert item.status == "ranked"
        assert item.score_breakdown["purity"] == pytest.approx(0.9)

    def test_latency_and_speed_filters(self) -> None:
        report = run_score(
            nodes=(make_node("p1", risk=10.0),),
            results=(make_ok_result(latency=900.0),),
        )
        assert report.proxies[0].filters_failed == ["latency"]
        report = run_score(
            nodes=(make_node("p1", risk=10.0),),
            results=(make_ok_result(speed=0.2),),
        )
        assert report.proxies[0].filters_failed == ["speed"]

    def test_speed_hard_gate_locked(self) -> None:
        report = run_score(
            nodes=(make_node("p1", risk=10.0),),
            results=(make_ok_result(speed=0.2),),
        )
        item = report.proxies[0]
        assert item.status == "filtered"
        assert "speed" in item.filters_failed

        relaxed = ScoringConfig(filters=ScoringFilters(require_speed=False))
        report = run_score(
            nodes=(make_node("p1", risk=10.0),),
            results=(make_ok_result(speed=0.2),),
            scoring=relaxed,
        )
        item = report.proxies[0]
        assert item.status == "ranked"
        assert "speed" not in item.filters_failed

    def test_critical_dimension_missing(self) -> None:
        report = run_score(
            nodes=(make_node("p1", risk=10.0),),
            results=(make_ok_result(speed=None),),
        )
        item = report.proxies[0]
        assert item.status == "pending"
        assert item.pending == ["speed_mb_s"]

        exclude = ScoringConfig(filters=ScoringFilters(missing="exclude"))
        report = run_score(
            nodes=(make_node("p1", risk=10.0),),
            results=(make_ok_result(speed=None),),
            scoring=exclude,
        )
        item = report.proxies[0]
        assert item.status == "filtered"
        assert item.filters_failed == ["missing_speed_mb_s"]


    def test_stability_dimension_from_history(self) -> None:
        report = run_score(
            nodes=(make_node("p1", risk=10.0),),
            results=(make_ok_result(),),
            proxy_history=history("p1", samples=5, availability=0.8),
        )
        item = report.proxies[0]
        assert item.score_breakdown["stability"] == pytest.approx(0.8)
        assert "stability_penalty" not in item.notes
        assert item.sample_count == 5
        assert item.availability_rate == pytest.approx(0.8)

    def test_low_sample_penalty_excludes_stability(self) -> None:
        report = run_score(
            nodes=(make_node("p1", risk=10.0),),
            results=(make_ok_result(),),
            proxy_history=history("p1", samples=1, availability=1.0),
        )
        item = report.proxies[0]
        assert item.status == "ranked"
        assert "stability" not in item.score_breakdown
        assert item.notes["stability_samples_insufficient"] is True
        assert item.notes["stability_penalty"] == pytest.approx(0.9)

    def test_stability_weight_shifts_score(self) -> None:
        stable = history("p1", samples=5, availability=1.0)
        weak = history("p1", samples=5, availability=0.2)
        scoring = ScoringConfig(
            weights=ScoringWeights(
                latency=0.4, speed=0.4, purity=0.0, stability=0.2, loss=0.0
            )
        )
        high = run_score(
            nodes=(make_node("p1", risk=0.0),),
            results=(make_ok_result(),),
            proxy_history=stable,
            scoring=scoring,
        ).proxies[0]
        low = run_score(
            nodes=(make_node("p1", risk=0.0),),
            results=(make_ok_result(),),
            proxy_history=weak,
            scoring=scoring,
        ).proxies[0]
        assert high.score > low.score
        assert high.score_breakdown["stability"] == pytest.approx(1.0)
        assert low.score_breakdown["stability"] == pytest.approx(0.2)

    def test_custom_weights_change_breakdown(self) -> None:
        heavy_speed = ScoringConfig(
            weights=ScoringWeights(
                latency=0.1, speed=0.8, purity=0.05, stability=0.05, loss=0.0
            )
        )
        heavy_latency = ScoringConfig(
            weights=ScoringWeights(
                latency=0.8, speed=0.1, purity=0.05, stability=0.05, loss=0.0
            )
        )
        fast_high_latency = make_ok_result("p1", latency=600.0, speed=20.0)
        speed_first = run_score(
            nodes=(make_node("p1", risk=10.0),),
            results=(fast_high_latency,),
            scoring=heavy_speed,
            proxy_history=history("p1", samples=5, availability=1.0),
        ).proxies[0]
        latency_first = run_score(
            nodes=(make_node("p1", risk=10.0),),
            results=(fast_high_latency,),
            scoring=heavy_latency,
            proxy_history=history("p1", samples=5, availability=1.0),
        ).proxies[0]
        assert speed_first.score > latency_first.score
        snap = speed_first.rule_snapshot
        assert snap["weights"]["speed"] == pytest.approx(0.8)
        assert snap["filters"]["min_speed_mb_s"] == pytest.approx(0.5)
        assert snap["filters"]["require_speed"] is True

    def test_speed_mbps_fallback_conversion(self) -> None:
        result = make_ok_result(speed=None)
        result.download_bytes = 1_000_000
        result.speed_mbps = 800.0
        report = run_score(
            nodes=(make_node("p1", risk=10.0),), results=(result,)
        )
        item = report.proxies[0]
        assert item.speed_mb_s == pytest.approx(100.0)

    def test_rank_order_and_tie_breaks(self) -> None:
        report = run_score(
            nodes=(
                make_node("p-slow", risk=10.0),
                make_node("p-fast", risk=10.0),
                make_node("p-mid", risk=10.0),
            ),
            results=(
                make_ok_result("p-slow", latency=600.0),
                make_ok_result("p-fast", latency=100.0),
                make_ok_result("p-mid", latency=300.0),
            ),
        )
        ranked = sorted(
            (item for item in report.proxies if item.status == "ranked"),
            key=lambda item: item.rank,
        )
        assert [item.item_id for item in ranked] == ["p-fast", "p-mid", "p-slow"]
        assert [item.rank for item in ranked] == [1, 2, 3]

        report = run_score(
            nodes=(make_node("pb", risk=10.0), make_node("pa", risk=10.0)),
            results=(make_ok_result("pb"), make_ok_result("pa")),
        )
        ranked = sorted(
            (item for item in report.proxies if item.status == "ranked"),
            key=lambda item: item.rank,
        )
        assert [item.item_id for item in ranked] == ["pa", "pb"]

        filtered = run_score(
            nodes=(make_node("p1", risk=10.0), make_node("p2", risk=10.0)),
            results=(make_ok_result("p1", latency=900.0), make_ok_result("p2")),
        )
        for item in filtered.proxies:
            if item.status != "ranked":
                assert item.rank == 0



class TestEndpointScoring:
    def test_ok_endpoint_ranked_with_full_dims(self) -> None:
        report = run_score(
            edges=(make_edge("e1", risk=10.0),),
            results=(make_eok("e1"),),
            endpoint_history=history("e1", samples=5, availability=0.9),
        )
        item = report.endpoints[0]
        assert item.status == "ranked"
        assert item.rank == 1
        assert set(item.score_breakdown) == {
            "compatibility",
            "latency",
            "speed",
            "loss",
            "purity",
            "stability",
        }
        assert item.score_breakdown["compatibility"] == 1.0
        assert item.score_breakdown["purity"] == pytest.approx(0.9)
        assert item.score_breakdown["stability"] == pytest.approx(0.9)
        assert item.host_compatible is True

    def test_endpoint_stability_insufficient_samples(self) -> None:
        report = run_score(
            edges=(make_edge("e1", risk=10.0),),
            results=(make_eok("e1"),),
            endpoint_history=history("e1", samples=1, availability=1.0),
        )
        item = report.endpoints[0]
        assert item.status == "ranked"
        assert "stability" not in item.score_breakdown
        assert item.notes["stability_samples_insufficient"] is True
        assert item.notes["stability_penalty"] == pytest.approx(0.9)

    def test_endpoint_purity_missing_pending(self) -> None:
        # CF edge probing assigns a neutral default risk when unknown, so
        # ranking can proceed; purity is never fabricated as "pure".
        report = run_score(
            edges=(make_edge("e1"),),
            results=(make_eok("e1"),),
        )
        item = report.endpoints[0]
        assert item.status == "ranked"
        assert item.notes.get("purity_source") == "cf_default_neutral"
        assert item.score_breakdown.get("purity") == pytest.approx(0.7)

    def test_endpoint_cf_anycast_neutral_purity(self) -> None:
        report = run_score(
            edges=(make_edge("e1", asn="AS13335 Cloudflare"),),
            results=(make_eok("e1"),),
        )
        item = report.endpoints[0]
        assert item.status == "ranked"
        assert item.risk == pytest.approx(CF_ANYCAST_NEUTRAL_RISK)
        assert item.score_breakdown["purity"] == pytest.approx(
            purity_score(CF_ANYCAST_NEUTRAL_RISK)
        )
        assert item.notes.get("purity_source") == "cf_anycast_neutral"

    def test_endpoint_max_risk_filter(self) -> None:
        report = run_score(
            edges=(make_edge("e1", risk=80.0),),
            results=(make_eok("e1"),),
        )
        item = report.endpoints[0]
        assert item.status == "filtered"
        assert "risk" in item.filters_failed

    def test_incompatible_endpoint_filtered(self) -> None:
        report = run_score(
            edges=(make_edge("e1", risk=10.0),),
            results=(make_eok("e1", host_compatible=False),),
        )
        item = report.endpoints[0]
        assert item.status == "filtered"
        assert item.filters_failed == ["compatibility"]

    def test_unknown_compatibility_is_critical_missing(self) -> None:
        report = run_score(
            edges=(make_edge("e1", risk=10.0),),
            results=(make_eok("e1", host_compatible=None),),
        )
        item = report.endpoints[0]
        assert item.status == "pending"
        assert item.pending == ["host_compatible"]

        exclude = ScoringConfig(filters=ScoringFilters(missing="exclude"))
        report = run_score(
            edges=(make_edge("e1", risk=10.0),),
            results=(make_eok("e1", host_compatible=None),),
            scoring=exclude,
        )
        assert report.endpoints[0].filters_failed == ["missing_host_compatible"]

    def test_country_filter_applies_to_endpoints(self) -> None:
        scoring = ScoringConfig(filters=ScoringFilters(allowed_countries=["US"]))
        report = run_score(
            edges=(make_edge("e1", risk=10.0, region="DE"),),
            results=(make_eok("e1"),),
            scoring=scoring,
        )
        assert report.endpoints[0].filters_failed == ["country"]

        report = run_score(
            edges=(make_edge("e1", risk=10.0, region="US"),),
            results=(make_eok("e1"),),
            scoring=scoring,
        )
        assert report.endpoints[0].status == "ranked"
        assert report.endpoints[0].country_code == "US"


    def test_country_from_cfst_region(self) -> None:
        report = run_score(
            edges=(make_edge("e1", risk=10.0),),
            results=(make_eok("e1", region="SG"),),
        )
        assert report.endpoints[0].country_code == "SG"

    def test_country_from_iata_region_beats_us_registration(self) -> None:
        """HKG must become HK, not Cloudflare-anycast IPinfo 'US'."""
        report = run_score(
            edges=(make_edge("e1", risk=10.0, country_code="US"),),
            results=(make_eok("e1", region="HKG"),),
        )
        assert report.endpoints[0].country_code == "HK"
        assert report.endpoints[0].region == "HKG"

    def test_country_from_iata_nrt_is_jp(self) -> None:
        report = run_score(
            edges=(make_edge("e1", risk=10.0),),
            results=(make_eok("e1", region="NRT"),),
        )
        assert report.endpoints[0].country_code == "JP"

    def test_country_from_geo_lookup(self) -> None:
        calls: list[str] = []

        def fake_geo(address: str):
            calls.append(address)
            return GeoResult(country_code="JP", asn="AS2497", isp="IIJ")

        report = run_score(
            edges=(make_edge("e1", risk=10.0),),
            results=(make_eok("e1"),),
            geo_lookup=fake_geo,
        )
        item = report.endpoints[0]
        assert item.country_code == "JP"
        assert calls == ["1.1.1.1"]

    def test_geo_lookup_failure_is_unknown(self) -> None:
        report = run_score(
            edges=(make_edge("e1", risk=10.0),),
            results=(make_eok("e1"),),
            geo_lookup=lambda address: None,
        )
        assert report.endpoints[0].country_code is None

    def test_endpoint_latency_and_speed_filters(self) -> None:
        report = run_score(
            edges=(make_edge("e1", risk=10.0),),
            results=(make_eok("e1", latency=900.0),),
        )
        assert report.endpoints[0].filters_failed == ["latency"]
        report = run_score(
            edges=(make_edge("e1", risk=10.0),),
            results=(make_eok("e1", speed=0.1),),
        )
        assert report.endpoints[0].filters_failed == ["speed"]

    def test_endpoint_speed_hard_gate(self) -> None:
        report = run_score(
            edges=(make_edge("e1", risk=10.0),),
            results=(make_eok("e1", speed=0.1),),
        )
        item = report.endpoints[0]
        assert item.status == "filtered"
        assert "speed" in item.filters_failed

        relaxed = ScoringConfig(filters=ScoringFilters(require_speed=False))
        report = run_score(
            edges=(make_edge("e1", risk=10.0),),
            results=(make_eok("e1", speed=0.1),),
            scoring=relaxed,
        )
        item = report.endpoints[0]
        assert item.status == "ranked"
        assert "speed" not in item.filters_failed

    def test_endpoint_custom_cf_weights(self) -> None:
        scoring = ScoringConfig(
            cf_weights=ScoringCfWeights(
                compatibility=0.1,
                latency=0.1,
                speed=0.1,
                loss=0.1,
                purity=0.5,
                stability=0.1,
            )
        )
        report = run_score(
            edges=(make_edge("e1", risk=0.0),),
            results=(make_eok("e1"),),
            endpoint_history=history("e1", samples=5, availability=1.0),
            scoring=scoring,
        )
        item = report.endpoints[0]
        assert item.rule_snapshot["cf_weights"]["purity"] == pytest.approx(0.5)
        assert item.score_breakdown["purity"] == pytest.approx(1.0)
        dirty = run_score(
            edges=(make_edge("e2", risk=50.0),),
            results=(make_eok("e2"),),
            endpoint_history=history("e2", samples=5, availability=1.0),
            scoring=scoring,
        ).endpoints[0]
        assert item.score > dirty.score

    def test_endpoint_missing_probe_pending(self) -> None:
        report = run_score(edges=(make_edge("e1", risk=10.0),))
        assert report.endpoints[0].status == "pending"
        assert report.endpoints[0].pending == ["probe"]


class TestGeoLookupHelper:
    def test_make_geo_lookup_caches_and_bounds(self) -> None:
        calls: list[str] = []

        def fake(ip: str, url: str, timeout: float) -> GeoResult:
            calls.append(ip)
            return GeoResult(country_code="US", asn="AS13335", isp="Cloudflare")

        lookup = make_geo_lookup(base_url="https://geo.example", lookup=fake)
        first = lookup("1.1.1.1")
        second = lookup("1.1.1.1")
        assert first is second
        assert calls == ["1.1.1.1"]
        assert lookup("not-an-ip") is None
        assert lookup("") is None

    def test_make_geo_lookup_without_base_url_is_offline(self) -> None:
        calls: list[str] = []

        def fake(ip: str, url: str, timeout: float) -> GeoResult:
            calls.append(ip)
            return GeoResult(country_code="US", asn="AS13335", isp="Cloudflare")

        lookup = make_geo_lookup(base_url="", lookup=fake)
        assert lookup("1.1.1.1") is None
        assert calls == []



class TestReport:
    def test_counts_and_round_trip(self) -> None:
        report = run_score(
            nodes=(make_node("p1", risk=10.0), make_node("p2", risk=10.0)),
            edges=(make_edge("e1", risk=10.0),),
            results=(
                make_ok_result("p1"),
                make_eok("e1", latency=900.0),
                make_ok_result("ghost"),
            ),
        )
        assert report.counts == {
            "proxies": 2,
            "endpoints": 1,
            "ranked": 1,
            "filtered": 1,
            "pending": 1,
            "issues": 1,
        }
        assert report.issues[0].item_id == "ghost"
        assert report.issues[0].kind == "proxy_node"
        assert report.issues[0].code == "probe_orphan"
        dumped = report.model_dump(mode="json")
        assert ScoreReport.model_validate(dumped) == report

    def test_duplicate_probe_results_first_wins(self) -> None:
        report = run_score(
            nodes=(make_node("p1", risk=10.0),),
            results=(make_ok_result(latency=100.0), make_ok_result(latency=700.0)),
        )
        assert report.proxies[0].latency_ms == pytest.approx(100.0)

    def test_endpoint_orphan_issue_kind(self) -> None:
        report = run_score(
            edges=(make_edge("e1", risk=10.0),),
            results=(make_eok("e1"), make_eok("ghost-e")),
        )
        assert report.issues[0].item_id == "ghost-e"
        assert report.issues[0].kind == "edge_endpoint"
        assert report.counts["issues"] == 1

    def test_default_scoring_config_has_cf_weights(self) -> None:
        config = ScoringConfig()
        assert config.cf_weights.compatibility == pytest.approx(0.25)
        assert config.cf_weights.latency == pytest.approx(0.17)
        assert config.cf_weights.speed == pytest.approx(0.17)
        assert config.cf_weights.loss == pytest.approx(0.11)
        assert config.cf_weights.purity == pytest.approx(0.15)
        assert config.cf_weights.stability == pytest.approx(0.15)
        assert config.filters.require_speed is True
        with pytest.raises(ValueError):
            ScoringCfWeights(compatibility=-1.0)
        with pytest.raises(ValueError):
            ScoringCfWeights(purity=-0.1)

    def test_rule_snapshot_records_weights_and_thresholds(self) -> None:
        report = run_score(
            nodes=(make_node("p1", risk=10.0),),
            results=(make_ok_result(),),
        )
        snap = report.rule_snapshot
        assert snap["weights"]["purity"] == pytest.approx(0.25)
        assert snap["weights"]["stability"] == pytest.approx(0.15)
        assert snap["cf_weights"]["purity"] == pytest.approx(0.15)
        assert snap["cf_weights"]["stability"] == pytest.approx(0.15)
        assert snap["filters"]["min_speed_mb_s"] == pytest.approx(0.5)
        assert snap["filters"]["max_risk"] == pytest.approx(50.0)
        assert snap["filters"]["require_speed"] is True
        assert snap["cf_anycast_neutral_risk"] == pytest.approx(30.0)


class TestDiversity:
    def _hk_edges(self, n: int, start: int = 1) -> tuple:
        edges = []
        results = []
        for i in range(start, start + n):
            item_id = f"hk-{i}"
            edges.append(make_edge(item_id, address=f"104.16.0.{i}"))
            results.append(
                make_eok(
                    item_id,
                    latency=20.0 + i,
                    speed=3.0 + i * 0.1,
                    region="HKG",
                )
            )
        return tuple(edges), tuple(results)

    def _sg_edge(self, item_id: str = "sg-1", rank_speed: float = 1.0) -> tuple:
        edge = make_edge(item_id, address="104.17.0.1")
        result = make_eok(
            item_id,
            latency=200.0,
            speed=rank_speed,
            region="SIN",
        )
        return (edge,), (result,)

    def test_per_region_quota_limits_country_winners(self) -> None:
        hk_edges, hk_results = self._hk_edges(8)
        sg_edges, sg_results = self._sg_edge()
        report = run_score(
            edges=hk_edges + sg_edges,
            results=hk_results + sg_results,
            scoring=ScoringConfig(
                diversity=ScoringDiversityConfig(
                    enabled=True, per_region=3, max_total=0
                )
            ),
        )
        ranked = [e for e in report.endpoints if e.status == "ranked"]
        demoted = [e for e in report.endpoints if e.status == "filtered"]
        assert len(ranked) == 4  # 3 HK + 1 SG
        countries = sorted(e.country_code for e in ranked)
        assert countries == ["HK", "HK", "HK", "SG"]
        assert all("region_quota" in e.filters_failed for e in demoted)
        assert all(e.rank == 0 for e in demoted)
        ranks = sorted(e.rank for e in ranked)
        assert ranks == [1, 2, 3, 4]

    def test_diversity_disabled_keeps_all_ranked(self) -> None:
        hk_edges, hk_results = self._hk_edges(8)
        report = run_score(
            edges=hk_edges,
            results=hk_results,
            scoring=ScoringConfig(
                diversity=ScoringDiversityConfig(enabled=False)
            ),
        )
        ranked = [e for e in report.endpoints if e.status == "ranked"]
        assert len(ranked) == 8

    def test_max_total_fills_with_global_best(self) -> None:
        hk_edges, hk_results = self._hk_edges(8)
        sg_edges, sg_results = self._sg_edge()
        report = run_score(
            edges=hk_edges + sg_edges,
            results=hk_results + sg_results,
            scoring=ScoringConfig(
                diversity=ScoringDiversityConfig(
                    enabled=True, per_region=1, max_total=4
                )
            ),
        )
        ranked = [e for e in report.endpoints if e.status == "ranked"]
        assert len(ranked) == 4
        by_country: dict[str, int] = {}
        for item in ranked:
            by_country[item.country_code] = by_country.get(item.country_code, 0) + 1
        # per_region=1 keeps the SG winner, then fill goes to best HK
        assert by_country.get("SG") == 1
        assert by_country.get("HK") == 3

    def test_unknown_country_groups_together(self) -> None:
        edges = []
        results = []
        for i in range(1, 6):
            item_id = f"u-{i}"
            edges.append(make_edge(item_id, address=f"203.0.113.{i}"))
            results.append(
                make_eok(item_id, latency=50.0 + i, speed=2.0 + i * 0.2, region="")
            )
        report = run_score(
            edges=tuple(edges),
            results=tuple(results),
            scoring=ScoringConfig(
                diversity=ScoringDiversityConfig(enabled=True, per_region=2)
            ),
        )
        ranked = [e for e in report.endpoints if e.status == "ranked"]
        assert len(ranked) == 2

    def test_diversity_in_rule_snapshot(self) -> None:
        report = run_score(
            edges=(make_edge("e1"),),
            results=(make_eok(region="HKG"),),
            scoring=ScoringConfig(
                diversity=ScoringDiversityConfig(enabled=True, per_region=15)
            ),
        )
        snap = report.rule_snapshot["diversity"]
        assert snap["enabled"] is True
        assert snap["per_region"] == 15
        assert snap["max_total"] == 0

    def test_diversity_disabled_by_default_keeps_all_winners(self) -> None:
        """A single strong region must not be capped when diversity is off."""
        edges = tuple(make_edge(f"e{i}", address=f"203.0.113.{i}") for i in range(1, 8))
        results = tuple(make_eok(f"e{i}", region="HKG") for i in range(1, 8))
        report = run_score(edges=edges, results=results)
        ranked = [e for e in report.endpoints if e.status == "ranked"]
        assert len(ranked) == 7
