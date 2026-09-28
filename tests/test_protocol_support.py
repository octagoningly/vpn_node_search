"""Protocol support level annotation (parse_only | probe_supported)."""

from __future__ import annotations

from datetime import datetime, timezone

from nodebench.core.protocol_support import (
    SUPPORT_RULE,
    is_real_probe_ok,
    parse_support_level,
    protocol_support_levels,
    support_report,
)
from nodebench.core.schema import (
    ParsedProxy,
    ProbeMode,
    ProbeStatus,
    ProtocolSupport,
    ProxyProbeResult,
)
from nodebench.normalize.canonical import normalize_proxy
from nodebench.parsers.uri import parse_uri

SRC = "src-support"
VLESS_UUID = "123e4567-e89b-12d3-a456-426614174000"
NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


def make_node(protocol: str = "vless"):
    node, _issue = normalize_proxy(
        ParsedProxy(
            source_id=SRC,
            protocol=protocol,
            server="192.0.2.10",
            port=443,
            transport="tcp",
            security="tls",
        ),
        SRC,
    )
    return node


def make_result(
    item_id: str,
    *,
    status: ProbeStatus = ProbeStatus.OK,
    probe_mode: ProbeMode = ProbeMode.REAL,
) -> ProxyProbeResult:
    kwargs: dict = {}
    if status is ProbeStatus.SKIPPED:
        kwargs["skipped_reason"] = "missing_binary"
        kwargs["attempts"] = 0
    else:
        kwargs["attempts"] = 1
    return ProxyProbeResult(
        run_id="20260928T120000Z-abcdef",
        runner_id="local:desktop-a",
        measured_at=NOW,
        status=status,
        probe_mode=probe_mode,
        backend="mihomo",
        item_id=item_id,
        total_latency_ms=80.0 if status is ProbeStatus.OK else None,
        download_bytes=1024 if status is ProbeStatus.OK else 0,
        speed_mb_s=1.5 if status is ProbeStatus.OK else None,
        **kwargs,
    )


def test_parse_results_default_to_parse_only():
    node, issue = parse_uri(
        "vless://{0}@192.0.2.10:443?security=tls#r".format(VLESS_UUID), SRC
    )
    assert issue is None
    assert node.protocol_support is ProtocolSupport.PARSE_ONLY
    assert parse_support_level() is ProtocolSupport.PARSE_ONLY


def test_normalized_node_carries_parse_only():
    node = make_node()
    assert node.protocol_support is ProtocolSupport.PARSE_ONLY


def test_levels_stay_parse_only_without_real_success():
    node = make_node()
    for result in (
        None,
        make_result(node.item_id, status=ProbeStatus.FAIL),
        make_result(
            node.item_id,
            status=ProbeStatus.SKIPPED,
            probe_mode=ProbeMode.NOT_RUN,
        ),
        make_result(
            node.item_id, status=ProbeStatus.OK, probe_mode=ProbeMode.SIMULATED
        ),
    ):
        results = [] if result is None else [result]
        levels = protocol_support_levels([node], results)
        assert levels == {"vless": "parse_only"}


def test_real_ok_probe_elevates_protocol():
    node = make_node()
    other = make_node(protocol="trojan")
    levels = protocol_support_levels(
        [node, other],
        [make_result(node.item_id)],
    )
    assert levels == {"vless": "probe_supported", "trojan": "parse_only"}


def test_is_real_probe_ok_requires_real_and_ok():
    assert is_real_probe_ok(make_result("n")) is True
    assert is_real_probe_ok(make_result("n", status=ProbeStatus.FAIL)) is False
    assert (
        is_real_probe_ok(make_result("n", probe_mode=ProbeMode.SIMULATED)) is False
    )
    assert is_real_probe_ok({"probe_mode": "real", "status": "ok"}) is True
    assert is_real_probe_ok({"probe_mode": "not_run", "status": "ok"}) is False


def test_support_report_shape_and_rule():
    node = make_node()
    report = support_report([node], [make_result(node.item_id)])
    assert report["rule"] == SUPPORT_RULE
    assert "real connectivity" in report["rule"]
    assert report["levels"] == {"vless": "probe_supported"}
    assert report["level_values"] == ["parse_only", "probe_supported"]


def test_support_report_without_nodes_is_empty_levels():
    report = support_report([], [])
    assert report["levels"] == {}
    assert report["rule"] == SUPPORT_RULE
