from __future__ import annotations

from pathlib import Path

from nodebench.parsers.csv import parse_endpoint_csv

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SRC = "src-csv"

HEADER_NINE = "IP地址,端口,回源端口,TLS,数据中心,地区,城市,TCP延迟(ms),速度(MB/s)\n"


def test_cf_candidates_nine_columns():
    text = (PROJECT_ROOT / "input" / "cf-candidates.csv").read_text(encoding="utf-8")
    endpoints, issues = parse_endpoint_csv(text, SRC)
    assert issues == []
    assert len(endpoints) == 3
    first = endpoints[0]
    assert first.source_id == SRC
    assert first.address == "192.0.2.81"
    assert first.port == 443
    assert first.tls is True
    assert first.target_host == ""
    assert first.remarks == ""
    assert set(first.params) == {
        "origin_port",
        "datacenter",
        "region",
        "city",
        "historical_latency_ms",
        "historical_speed_mb_s",
    }
    assert first.params["origin_port"] == 80
    assert first.params["historical_latency_ms"] == 15.6
    assert first.params["historical_speed_mb_s"] == 7.82
    assert first.params["region"] == "HK"
    assert first.params["city"] == "Hong Kong"
    assert endpoints[2].tls is False
    assert endpoints[2].address == "203.0.113.7"


def test_latency_and_speed_stay_historical_not_measurements():
    text = (PROJECT_ROOT / "input" / "cf-candidates.csv").read_text(encoding="utf-8")
    endpoints, issues = parse_endpoint_csv(text, SRC)
    assert issues == []
    for node in endpoints:
        dumped = node.model_dump()
        assert "latency_ms" not in dumped
        assert "speed_mb_s" not in dumped
        assert set(node.params) & {"historical_latency_ms", "historical_speed_mb_s"}
        for key, value in node.params.items():
            if key.startswith("historical_"):
                assert isinstance(value, float)


def test_ip_port_header():
    text = (PROJECT_ROOT / "input" / "sample-endpoints.csv").read_text(
        encoding="utf-8"
    )
    endpoints, issues = parse_endpoint_csv(text, SRC)
    assert issues == []
    assert len(endpoints) == 3
    assert endpoints[0].address == "192.0.2.40"
    assert endpoints[0].port == 443
    assert endpoints[0].tls is False
    assert endpoints[0].params == {"origin_port": 443}
    assert endpoints[2].port == 8443


def test_headerless_two_columns():
    text = "192.0.2.60,1080\n198.51.100.60,8080\n"
    endpoints, issues = parse_endpoint_csv(text, SRC)
    assert issues == []
    assert len(endpoints) == 2
    assert endpoints[1].address == "198.51.100.60"
    assert endpoints[1].port == 8080


def test_column_count_mismatch():
    text = "192.0.2.61,443,extra\n"
    endpoints, issues = parse_endpoint_csv(text, SRC)
    assert endpoints == []
    assert issues[0].code == "invalid_row"
    assert issues[0].raw_ref == "line:1"


def test_invalid_ip_reported():
    text = "not_a_host!.local,443\n"
    endpoints, issues = parse_endpoint_csv(text, SRC)
    assert endpoints == []
    assert issues[0].code == "invalid_row"
    assert issues[0].raw_ref == "line:1"


def test_invalid_tls_flag_reported():
    text = HEADER_NINE + "192.0.2.63,443,80,maybe,香港,HK,Hong Kong,15.6,7.82\n"
    endpoints, issues = parse_endpoint_csv(text, SRC)
    assert endpoints == []
    assert issues[0].code == "invalid_row"


def test_port_out_of_range_reported():
    for bad_port in ("65536", "0", "abc"):
        text = "192.0.2.64,{0}\n".format(bad_port)
        endpoints, issues = parse_endpoint_csv(text, SRC)
        assert endpoints == [], bad_port
        assert issues[0].code == "invalid_port", bad_port
        assert issues[0].raw_ref == "line:1"


def test_bracketed_ipv6_address():
    text = "[2001:db8::1],443\n"
    endpoints, issues = parse_endpoint_csv(text, SRC)
    assert issues == []
    assert endpoints[0].address == "2001:db8::1"
    assert endpoints[0].port == 443


def test_embedded_port_fallback():
    text = "[2001:db8::2]:8443,\n"
    endpoints, issues = parse_endpoint_csv(text, SRC)
    assert issues == []
    assert endpoints[0].address == "2001:db8::2"
    assert endpoints[0].port == 8443


def test_byte_order_mark_before_header():
    text = "\ufeffip,port\n192.0.2.62,443\n"
    endpoints, issues = parse_endpoint_csv(text, SRC)
    assert issues == []
    assert endpoints[0].address == "192.0.2.62"
    assert endpoints[0].port == 443


def test_empty_and_header_only_inputs():
    assert parse_endpoint_csv("", SRC) == ([], [])
    endpoints, issues = parse_endpoint_csv("ip,port\n", SRC)
    assert endpoints == []
    assert issues == []


def test_blank_rows_skipped():
    text = "ip,port\n\n192.0.2.65,443\n   \n198.51.100.65,80\n"
    endpoints, issues = parse_endpoint_csv(text, SRC)
    assert issues == []
    assert len(endpoints) == 2
