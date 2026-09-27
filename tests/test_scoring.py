from __future__ import annotations

import pytest

from nodebench.core.config import ScoringCfWeights, ScoringConfig, ScoringFilters
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
            nodes=(make_node(),),
            results=(make_ok_result(),),
        )
        assert report.counts["ranked"] == 1
        item = report.proxies[0]
        assert item.status == "ranked"
        assert item.rank == 1
        assert 0.0 < item.score <= 1.0
        assert set(item.score_breakdown) == {"latency", "speed", "loss"}
        assert item.probe_status == "ok"
        assert item.probe_mode == "real"
        assert item.observed_at is not None
        assert item.scoring_version == SCORING_VERSION
        assert item.rule_snapshot["scoring_version"] == SCORING_VERSION
        assert item.notes["stability_samples_insufficient"] is True
        assert item.notes["stability_penalty"] == pytest.approx(0.9)

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
        report = run_score(nodes=(make_node(),), results=(simulated,))
        item = report.proxies[0]
        assert item.status == "filtered"
        assert item.filters_failed == ["probe_mode"]

        relaxed = ScoringConfig(
            filters=ScoringFilters(require_real_probe_success=False)
        )
        report = run_score(
            nodes=(make_node(),), results=(simulated,), scoring=relaxed
        )
        assert report.proxies[0].status == "ranked"

    def test_not_run_mode_filtered_when_real_required(self) -> None:
        not_run = make_ok_result()
        not_run.probe_mode = ProbeMode.NOT_RUN
        report = run_score(nodes=(make_node(),), results=(not_run,))
        item = report.proxies[0]
        assert item.status == "filtered"
        assert item.filters_failed == ["probe_mode"]

        relaxed = ScoringConfig(
            filters=ScoringFilters(require_real_probe_success=False)
        )
        report = run_score(nodes=(make_node(),), results=(not_run,), scoring=relaxed)
        assert report.proxies[0].status == "ranked"

    def test_country_filter(self) -> None:
        scoring = ScoringConfig(filters=ScoringFilters(allowed_countries=["US"]))
        report = run_score(
            nodes=(make_node("p1", country="DE"),),
            results=(make_ok_result("p1"),),
            scoring=scoring,
        )
        assert report.proxies[0].filters_failed == ["country"]
        report = run_score(
            nodes=(make_node("p1", country="US"),),
            results=(make_ok_result("p1"),),
            scoring=scoring,
        )
        assert report.proxies[0].status == "ranked"
        report = run_score(
            nodes=(make_node("p1"),),
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
            nodes=(make_node(),),
            results=(make_ok_result(latency=900.0),),
        )
        assert report.proxies[0].filters_failed == ["latency"]
        report = run_score(
            nodes=(make_node(),),
            results=(make_ok_result(speed=0.2),),
        )
        assert report.proxies[0].filters_failed == ["speed"]

    def test_critical_dimension_missing(self) -> None:
        report = run_score(
            nodes=(make_node(),),
            results=(make_ok_result(speed=None),),
        )
        item = report.proxies[0]
        assert item.status == "pending"
        assert item.pending == ["speed_mb_s"]

        exclude = ScoringConfig(filters=ScoringFilters(missing="exclude"))
        report = run_score(
            nodes=(make_node(),),
            results=(make_ok_result(speed=None),),
            scoring=exclude,
        )
        item = report.proxies[0]
        assert item.status == "filtered"
        assert item.filters_failed == ["missing_speed_mb_s"]

    def test_stability_dimension_from_history(self) -> None:
        report = run_score(
            nodes=(make_node(),),
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
            nodes=(make_node(),),
            results=(make_ok_result(),),
            proxy_history=history("p1", samples=1, availability=1.0),
        )
        item = report.proxies[0]
        assert item.status == "ranked"
        assert "stability" not in item.score_breakdown
        assert item.notes["stability_samples_insufficient"] is True
        assert item.notes["stability_penalty"] == pytest.approx(0.9)

    def test_speed_mbps_fallback_conversion(self) -> None:
        result = make_ok_result(speed=None)
        result.download_bytes = 1_000_000
        result.speed_mbps = 800.0
        report = run_score(nodes=(make_node(),), results=(result,))
        item = report.proxies[0]
        assert item.speed_mb_s == pytest.approx(100.0)

    def test_rank_order_and_tie_breaks(self) -> None:
        report = run_score(
            nodes=(make_node("p-slow"), make_node("p-fast"), make_node("p-mid")),
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
            nodes=(make_node("pb"), make_node("pa")),
            results=(make_ok_result("pb"), make_ok_result("pa")),
        )
        ranked = sorted(
            (item for item in report.proxies if item.status == "ranked"),
            key=lambda item: item.rank,
        )
        assert [item.item_id for item in ranked] == ["pa", "pb"]

        filtered = run_score(
            nodes=(make_node("p1"), make_node("p2")),
            results=(make_ok_result("p1", latency=900.0), make_ok_result("p2")),
        )
        for item in filtered.proxies:
            if item.status != "ranked":
                assert item.rank == 0


class TestEndpointScoring:
    def test_ok_endpoint_ranked_with_compatibility(self) -> None:
        report = run_score(edges=(make_edge(),), results=(make_eok(),))
        item = report.endpoints[0]
        assert item.status == "ranked"
        assert item.rank == 1
        assert set(item.score_breakdown) == {
            "compatibility",
            "latency",
            "speed",
            "loss",
        }
        assert item.score_breakdown["compatibility"] == 1.0
        assert item.host_compatible is True

    def test_incompatible_endpoint_filtered(self) -> None:
        report = run_score(
            edges=(make_edge(),), results=(make_eok(host_compatible=False),)
        )
        item = report.endpoints[0]
        assert item.status == "filtered"
        assert item.filters_failed == ["compatibility"]

    def test_unknown_compatibility_is_critical_missing(self) -> None:
        report = run_score(
            edges=(make_edge(),), results=(make_eok(host_compatible=None),)
        )
        item = report.endpoints[0]
        assert item.status == "pending"
        assert item.pending == ["host_compatible"]

        exclude = ScoringConfig(filters=ScoringFilters(missing="exclude"))
        report = run_score(
            edges=(make_edge(),),
            results=(make_eok(host_compatible=None),),
            scoring=exclude,
        )
        assert report.endpoints[0].filters_failed == ["missing_host_compatible"]

    def test_country_filter_does_not_apply_to_endpoints(self) -> None:
        scoring = ScoringConfig(filters=ScoringFilters(allowed_countries=["US"]))
        report = run_score(
            edges=(make_edge(),), results=(make_eok(),), scoring=scoring
        )
        assert report.endpoints[0].status == "ranked"

    def test_endpoint_latency_and_speed_filters(self) -> None:
        report = run_score(
            edges=(make_edge(),), results=(make_eok(latency=900.0),)
        )
        assert report.endpoints[0].filters_failed == ["latency"]
        report = run_score(
            edges=(make_edge(),), results=(make_eok(speed=0.1),)
        )
        assert report.endpoints[0].filters_failed == ["speed"]

    def test_endpoint_missing_probe_pending(self) -> None:
        report = run_score(edges=(make_edge(),))
        assert report.endpoints[0].status == "pending"
        assert report.endpoints[0].pending == ["probe"]


class TestReport:
    def test_counts_and_round_trip(self) -> None:
        report = run_score(
            nodes=(make_node("p1"), make_node("p2")),
            edges=(make_edge("e1"),),
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
            nodes=(make_node(),),
            results=(make_ok_result(latency=100.0), make_ok_result(latency=700.0)),
        )
        assert report.proxies[0].latency_ms == pytest.approx(100.0)

    def test_endpoint_orphan_issue_kind(self) -> None:
        report = run_score(
            edges=(make_edge("e1"),),
            results=(make_eok("e1"), make_eok("ghost-e")),
        )
        assert report.issues[0].item_id == "ghost-e"
        assert report.issues[0].kind == "edge_endpoint"
        assert report.counts["issues"] == 1

    def test_default_scoring_config_has_cf_weights(self) -> None:
        config = ScoringConfig()
        assert config.cf_weights.compatibility == pytest.approx(0.35)
        assert config.cf_weights.latency == pytest.approx(0.25)
        assert config.cf_weights.speed == pytest.approx(0.25)
        assert config.cf_weights.loss == pytest.approx(0.15)
        with pytest.raises(ValueError):
            ScoringCfWeights(compatibility=-1.0)
