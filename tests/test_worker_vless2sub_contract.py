"""Lock the cmliu/WorkerVless2sub ADDAPI / ADDCSV consumer contract.

Fixed samples mirror:

* README format notes (ADDAPI line, ADDCSV nine columns, DLS speed filter)
* https://raw.githubusercontent.com/cmliu/WorkerVless2sub/main/addressescsv.csv
* https://raw.githubusercontent.com/cmliu/WorkerVless2sub/main/addressesapi.txt
* https://raw.githubusercontent.com/cmliu/WorkerVless2sub/main/addressesipv6api.txt

Large upstream files are not vendored into the repository.
"""

from __future__ import annotations

import re

from nodebench.core.schema import EdgeEndpoint, RankedEndpoint
from nodebench.exporters.cf_addapi import (
    DEFAULT_ADDAPI_REMARK_TEMPLATE,
    build_addapi,
    format_addapi_remark,
)
from nodebench.exporters.cf_addcsv import build_addcsv
from nodebench.exporters.consumer_hints import build_consumer_hints
from nodebench.parsers.csv import (
    HEADER_FULL,
    parse_endpoint_csv,
    parse_endpoint_lines,
)

SRC = "contract"

# Exact header from addressescsv.csv (iptest nine columns).
WORKERVLESS2SUB_ADDCSV_HEADER = [
    "IP地址",
    "端口",
    "回源端口",
    "TLS",
    "数据中心",
    "地区",
    "城市",
    "TCP延迟(ms)",
    "速度(MB/s)",
]

# Representative rows from addressescsv.csv (values shortened, layout kept).
SAMPLE_ADDCSV_ROWS = [
    ("130.61.203.115", 8443, 443, True, "FRA", "Europe", "Frankfurt", 195, 11.60),
    ("138.3.220.4", 2082, 2082, False, "NRT", "Asia Pacific", "Tokyo", 73, 11.59),
]

# Representative lines from addressesapi.txt / addressesipv6api.txt.
SAMPLE_ADDAPI_LINES = [
    "150.230.206.130:443#JP",
    "151.145.67.90:8443#JP",
    "[2606:4700:3037:e1:a64b:6580:941f:e09]:80#官方优选IPv6",
    "icook.tw:2053#优选域名",
    "cloudflare.cfgo.cc#优选官方线路",
]

ADDAPI_LINE_PATTERN = re.compile(
    r"^(?P<host>[^\[\]:]+|\[[0-9a-fA-F:]+\]):(?P<port>\d{1,5})(?:#(?P<remark>\S.*))?$"
)


def test_addcsv_header_is_the_iptest_nine_column_contract():
    assert HEADER_FULL == WORKERVLESS2SUB_ADDCSV_HEADER
    assert len(HEADER_FULL) == 9
    # column order is part of the contract
    assert HEADER_FULL[0] == "IP地址"
    assert HEADER_FULL[1] == "端口"
    assert HEADER_FULL[2] == "回源端口"
    assert HEADER_FULL[3] == "TLS"
    assert HEADER_FULL[7] == "TCP延迟(ms)"
    assert HEADER_FULL[8] == "速度(MB/s)"


def test_addcsv_export_locks_bool_latency_one_decimal_speed_two():
    text = build_addcsv(SAMPLE_ADDCSV_ROWS)
    lines = text.splitlines()
    assert lines[0] == ",".join(WORKERVLESS2SUB_ADDCSV_HEADER)
    # booleans are lowercase true/false
    assert lines[1].endswith(",true,") or ",true," in lines[1]
    assert ",false," in lines[2]
    # latency one decimal, speed two decimals
    assert lines[1].endswith(",195.0,11.60")
    assert lines[2].endswith(",73.0,11.59")
    # no invented values on empty measurement cells
    empty = build_addcsv([("198.51.100.8", 80, "", False, "", "", "", None, None)])
    assert empty.splitlines()[1] == "198.51.100.8,80,,false,,,,,"


def test_addcsv_roundtrip_is_lossless_through_parse_endpoint_csv():
    text = build_addcsv(SAMPLE_ADDCSV_ROWS)
    endpoints, issues = parse_endpoint_csv(text, SRC)
    assert issues == []
    assert len(endpoints) == len(SAMPLE_ADDCSV_ROWS)
    for node, row in zip(endpoints, SAMPLE_ADDCSV_ROWS):
        address, port, origin, tls, dc, region, city, latency, speed = row
        assert node.address == address
        assert node.port == port
        assert node.tls is tls
        assert node.params["origin_port"] == origin
        assert node.params["datacenter"] == dc
        assert node.params["region"] == region
        assert node.params["city"] == city
        assert node.params["historical_latency_ms"] == float(latency)
        assert node.params["historical_speed_mb_s"] == float(speed)
        # history never lands in measurement-shaped top-level fields
        dumped = node.model_dump()
        assert "latency_ms" not in dumped
        assert "speed_mb_s" not in dumped


def test_addapi_line_format_ipv4_domain_and_ipv6():
    text = build_addapi(
        [
            ("150.230.206.130", 443, "JP"),
            ("icook.tw", 2053, "优选域名"),
            ("2606:4700:3037:e1:a64b:6580:941f:e09", 80, "官方优选IPv6"),
            ("cloudflare.cfgo.cc", 443, ""),
        ]
    )
    lines = text.splitlines()
    assert lines == [
        "150.230.206.130:443#JP",
        "icook.tw:2053#优选域名",
        "[2606:4700:3037:e1:a64b:6580:941f:e09]:80#官方优选IPv6",
        "cloudflare.cfgo.cc:443",
    ]
    for line in lines:
        match = ADDAPI_LINE_PATTERN.match(line)
        assert match is not None, line


def test_addapi_lines_parse_back_with_remarks_and_ports():
    endpoints, issues = parse_endpoint_lines("\n".join(SAMPLE_ADDAPI_LINES) + "\n", SRC)
    assert issues == []
    by_address = {node.address: node for node in endpoints}
    assert by_address["150.230.206.130"].port == 443
    assert by_address["150.230.206.130"].remarks == "JP"
    assert by_address["151.145.67.90"].port == 8443
    assert by_address["2606:4700:3037:e1:a64b:6580:941f:e09"].port == 80
    assert by_address["2606:4700:3037:e1:a64b:6580:941f:e09"].remarks == "官方优选IPv6"
    assert by_address["icook.tw"].port == 2053
    assert by_address["icook.tw"].remarks == "优选域名"
    assert by_address["cloudflare.cfgo.cc"].port == 443
    assert by_address["cloudflare.cfgo.cc"].remarks == "优选官方线路"
    for node in endpoints:
        assert node.tls is True


def test_addapi_ipv6_bracket_form_is_required_in_export():
    text = build_addapi([("2001:db8::7", 8443, "v6")])
    assert text.splitlines() == ["[2001:db8::7]:8443#v6"]


def test_addapi_score_summary_remark_matches_line_pattern_and_parses_back():
    """#speed-purity-stability-country is a legal ADDAPI alias for v2rayN."""
    remarks = [
        format_addapi_remark(
            speed_mb_s=4.6, purity=0.85, stability=0.72, country_code="SG"
        ),
        format_addapi_remark(
            speed_mb_s=3.8, purity=0.80, stability=0.65, country_code="JP"
        ),
    ]
    assert remarks == ["4.6-0.85-0.72-新加坡", "3.8-0.80-0.65-日本"]
    text = build_addapi(
        [
            ("104.17.29.227", 8443, remarks[0]),
            ("139.162.41.109", 443, remarks[1]),
            ("2001:db8::7", 80, format_addapi_remark(speed_mb_s=1.0)),
        ]
    )
    lines = text.splitlines()
    assert lines[0] == "104.17.29.227:8443#4.6-0.85-0.72-新加坡"
    assert lines[1] == "139.162.41.109:443#3.8-0.80-0.65-日本"
    assert lines[2] == "[2001:db8::7]:80#1.0-------??"
    for line in lines:
        match = ADDAPI_LINE_PATTERN.match(line)
        assert match is not None, line
        assert match.group("remark")

    endpoints, issues = parse_endpoint_lines("\n".join(lines) + "\n", SRC)
    assert issues == []
    assert endpoints[0].remarks == "4.6-0.85-0.72-新加坡"
    assert endpoints[1].remarks == "3.8-0.80-0.65-日本"


def test_addapi_score_summary_placeholder_template_and_unknowns():
    assert (
        DEFAULT_ADDAPI_REMARK_TEMPLATE
        == "speed-purity-stability-country"
    )
    assert (
        format_addapi_remark(
            speed_mb_s=4.6,
            purity=0.85,
            stability=0.72,
            country_code="SG",
            template="{speed}-{purity}-{stability}-{country}",
        )
        == "4.6-0.85-0.72-新加坡"
    )
    assert format_addapi_remark(speed_mb_s=2.0) == "2.0-------??"
    assert (
        format_addapi_remark(
            speed_mb_s=2.0, purity=0.5, stability=None, country_code=None
        )
        == "2.0-0.50----??"
    )


def test_consumer_hints_cover_adddapi_addcsv_dls_and_user_fields():
    hints = build_consumer_hints(dls_min_speed_mb_s=0.5, endpoint_count=2, measured_endpoint_count=1)
    assert hints["files"]["addapi"]["path"] == "cf-addapi.txt"
    assert hints["files"]["addapi"]["env"] == "ADDAPI"
    assert hints["files"]["addcsv"]["path"] == "cf-addcsv.csv"
    assert hints["files"]["addcsv"]["env"] == "ADDCSV"
    assert hints["files"]["addcsv"]["format"].startswith("iptest-style nine columns")
    assert hints["dls"]["unit"] == "MB/s"
    assert hints["dls"]["field"] == "速度(MB/s)"
    assert hints["dls"]["suggested_min"] == 0.5
    assert set(hints["subscription"]["user_required_fields"]) == {
        "host",
        "uuid",
        "path",
        "sni",
    }
    assert hints["subscription"]["generates_fake_subscription"] is False
    assert "fake subscription" in hints["subscription"]["note"].lower() or "does not generate" in hints["subscription"]["note"]
    assert hints["counts"]["endpoints"] == 2
    assert hints["counts"]["measured_this_run"] == 1


def test_export_does_not_present_historical_values_as_this_run_measurements():
    """Unmeasured endpoints export empty speed/latency, never imported history."""
    edge = EdgeEndpoint(
        item_id="e-hist",
        kind="edge_endpoint",
        fingerprint="fp",
        fingerprint_version=1,
        address="198.51.100.7",
        port=443,
        tls=True,
        params={
            "historical_latency_ms": 12.0,
            "historical_speed_mb_s": 9.5,
            "origin_port": 80,
        },
    )
    ranked = RankedEndpoint(
        item_id="e-hist",
        status="pending",
        scoring_version="1",
        runner_id="local:test",
        probe_status="not_run",
        probe_mode="not_run",
        address=edge.address,
        port=edge.port,
        tls=edge.tls,
    )
    text = build_addcsv(
        [
            [
                edge.address,
                edge.port,
                edge.params.get("origin_port", ""),
                edge.tls,
                "",
                "",
                "",
                ranked.latency_ms,
                ranked.speed_mb_s,
            ]
        ]
    )
    body = text.splitlines()[1]
    # measurement columns are empty because this run measured nothing
    assert body.endswith(",,,")
    assert "12.0" not in body
    assert "9.5" not in body
    # origin port still round-trips as endpoint metadata
    assert body.startswith("198.51.100.7,443,80,true,")
