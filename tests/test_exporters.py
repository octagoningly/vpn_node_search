from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote

import pytest
import yaml

from nodebench.core.context import (
    clear_registered_secrets,
    register_secrets,
    registered_secrets,
)
from nodebench.core.schema import (
    EdgeEndpoint,
    ProxyNode,
    RankedEndpoint,
    RankedProxy,
)
from nodebench.exporters import (
    MANIFEST_NAME,
    REPORT_NAME,
    build_clash_config,
    build_clash_proxy,
    build_export,
    build_manifest,
    build_proxy_uri,
    build_raw_text,
    report_bytes,
    scan_files,
    scan_text,
)
from nodebench.exporters.cf_addapi import (
    CF_ADDAPI_NAME,
    DEFAULT_ADDAPI_REMARK_TEMPLATE,
    addapi_remark_from_ranked,
    build_addapi,
    format_addapi_remark,
)
from nodebench.exporters.cf_addcsv import CF_ADDCSV_NAME, build_addcsv
from nodebench.exporters.clash import PROXY_CLASH_NAME
from nodebench.exporters.raw import PROXY_RAW_NAME
from nodebench.exporters.uri import (
    REASON_MISSING_CREDENTIALS,
    REASON_UNSUPPORTED_PROTOCOL,
)
from nodebench.parsers.csv import HEADER_FULL
from nodebench.parsers.yaml_clash import parse_clash_yaml

RUN_ID = "20260101T000000Z-abcdef"
RUNNER = "local:desktop-a"
UUID = "123e4567-e89b-12d3-a456-426614174000"


@pytest.fixture(autouse=True)
def _clean_secrets():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def make_node(item_id: str = "p1", **kwargs: object) -> ProxyNode:
    payload: dict[str, object] = {
        "item_id": item_id,
        "kind": "proxy_node",
        "fingerprint": "fp-{0}".format(item_id),
        "fingerprint_version": 1,
        "protocol": "vless",
        "server": "192.0.2.10",
        "port": 443,
        "transport": "tcp",
        "security": "tls",
        "remarks": "sample-node",
        "source_ids": ["local:sample-uri-list.txt"],
    }
    payload.update(kwargs)
    node = ProxyNode(**payload)  # type: ignore[arg-type]
    if "secrets" not in kwargs:
        node.secrets.setdefault("uuid", UUID)
    return node


def make_edge(item_id: str = "e1", **kwargs: object) -> EdgeEndpoint:
    payload: dict[str, object] = {
        "item_id": item_id,
        "kind": "edge_endpoint",
        "fingerprint": "fp-{0}".format(item_id),
        "fingerprint_version": 1,
        "address": "198.51.100.7",
        "port": 443,
        "tls": True,
        "params": {"datacenter": "SJC", "region": "US", "city": "San Jose"},
        "remarks": "edge-a",
        "source_ids": ["local:sample-endpoints.csv"],
    }
    payload.update(kwargs)
    return EdgeEndpoint(**payload)  # type: ignore[arg-type]


def make_ranked_proxy(item_id: str = "p1", **kwargs: object) -> RankedProxy:
    payload: dict[str, object] = {
        "item_id": item_id,
        "status": "ranked",
        "rank": 1,
        "score": 0.9,
        "scoring_version": "1",
        "runner_id": RUNNER,
        "probe_status": "ok",
        "probe_mode": "real",
        "sample_count": 4,
        "latency_ms": 120.0,
        "speed_mb_s": 5.0,
        "protocol": "vless",
        "source_ids": ["local:sample-uri-list.txt"],
        "remarks": "sample-node",
    }
    payload.update(kwargs)
    return RankedProxy(**payload)  # type: ignore[arg-type]


def make_ranked_endpoint(item_id: str = "e1", **kwargs: object) -> RankedEndpoint:
    payload: dict[str, object] = {
        "item_id": item_id,
        "status": "ranked",
        "rank": 1,
        "score": 0.8,
        "scoring_version": "1",
        "runner_id": RUNNER,
        "probe_status": "ok",
        "probe_mode": "real",
        "sample_count": 4,
        "latency_ms": 30.0,
        "speed_mb_s": 8.0,
        "address": "198.51.100.7",
        "port": 443,
        "tls": True,
        "source_ids": ["local:sample-endpoints.csv"],
        "remarks": "edge-a",
    }
    payload.update(kwargs)
    return RankedEndpoint(**payload)  # type: ignore[arg-type]


def make_report(**kwargs: object) -> dict:
    report: dict = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "runner_id": RUNNER,
        "profile": "local",
        "generated_at": "2026-01-01T00:00:00Z",
        "status": "ok",
        "stages": {},
        "stages_pending": ["inspect", "persist", "score", "export", "publish"],
        "counts": {
            "proxy_nodes": 1,
            "edge_endpoints": 1,
            "sources_total": 1,
            "sources_failed": 0,
        },
    }
    report.update(kwargs)
    return report


def query_keys(uri: str) -> list[str]:
    head = uri.split("?", 1)[1]
    head = head.split("#", 1)[0]
    return [part.split("=", 1)[0] for part in head.split("&") if part]


# --- URI -----------------------------------------------------------------


def test_build_proxy_uri_orders_query_parameters():
    node = make_node(
        transport="ws",
        params={
            "zz": "last",
            "aa": "first",
            "plugin": "obfs",
            "headerType": "http",
            "serviceName": "svc",
            "cipher": "aes-128-gcm",
            "reality_short_id": "abcd",
            "reality_public_key": "public-key-value",
            "alter_id": 2,
            "fp": "chrome",
            "flow": "xtls-rprx-vision",
            "host": "node.example.test",
            "path": "/ws",
            "alpn": "h2,http/1.1",
            "sni": "node.example.test",
        },
    )
    uri, reason = build_proxy_uri(node)
    assert reason == ""
    assert uri is not None
    assert query_keys(uri) == [
        "type",
        "security",
        "sni",
        "alpn",
        "path",
        "host",
        "flow",
        "fp",
        "aid",
        "pbk",
        "sid",
        "scy",
        "serviceName",
        "headerType",
        "plugin",
        "aa",
        "zz",
    ]


def test_build_proxy_uri_swallows_credential_query_keys():
    node = make_node(
        params={
            "uuid": "leaked-uuid",
            "password": "leaked-password",
            "token": "leaked-token",
            "auth": "leaked-auth",
            "encryption": "auto",
            "network": "ws",
            "net": "ws",
            "tls": "1",
        }
    )
    uri, _reason = build_proxy_uri(node)
    assert uri is not None
    keys = set(query_keys(uri))
    for name in (
        "uuid",
        "password",
        "token",
        "auth",
        "encryption",
        "network",
        "net",
        "tls",
    ):
        assert name not in keys
    assert "leaked-password" not in uri


def test_build_proxy_uri_keeps_credentials_out_of_query_but_in_userinfo():
    node = make_node(protocol="trojan", secrets={"password": "hunter2"})
    uri, _reason = build_proxy_uri(node)
    assert uri is not None
    assert uri.startswith("trojan://")
    userinfo = uri[len("trojan://") :].split("@", 1)[0]
    assert userinfo == "hunter2"
    assert "type=" in uri


def test_build_proxy_uri_skips_missing_credentials():
    node = make_node(secrets={})
    uri, reason = build_proxy_uri(node)
    assert uri is None
    assert reason == REASON_MISSING_CREDENTIALS


def test_build_proxy_uri_skips_unsupported_protocol():
    node = make_node(protocol="socks5")
    uri, reason = build_proxy_uri(node)
    assert uri is None
    assert reason == REASON_UNSUPPORTED_PROTOCOL


def test_build_proxy_uri_brackets_ipv6_server():
    node = make_node(server="2001:db8::1")
    uri, _reason = build_proxy_uri(node)
    assert uri is not None
    assert "@[2001:db8::1]:443?" in uri


def test_build_proxy_uri_ss_userinfo_is_base64_method_password():
    node = make_node(
        protocol="ss",
        secrets={"password": "p@ss", "cipher": "aes-256-gcm"},
    )
    uri, _reason = build_proxy_uri(node)
    assert uri is not None
    userinfo = uri[len("ss://") :].split("@", 1)[0]
    decoded = base64.b64decode(unquote(userinfo)).decode("utf-8")
    assert decoded == "aes-256-gcm:p@ss"


def test_build_proxy_uri_vmess_is_base64_json():
    node = make_node(protocol="vmess", remarks="tokyo node", params={"alter_id": 0})
    uri, _reason = build_proxy_uri(node)
    assert uri is not None
    payload = json.loads(base64.b64decode(uri[len("vmess://") :]).decode("utf-8"))
    assert payload["v"] == "2"
    assert payload["add"] == "192.0.2.10"
    assert payload["port"] == "443"
    assert payload["id"] == UUID
    assert payload["ps"] == "tokyo node"
    assert payload["tls"] == "tls"


def test_build_raw_text_joins_with_newlines():
    assert build_raw_text([]) == ""
    assert build_raw_text(["a", "b"]) == "a\nb\n"


# --- clash ---------------------------------------------------------------


def test_build_clash_config_empty_document():
    assert build_clash_config([]) == "proxies: []\n"


def test_build_clash_proxy_emits_core_keys_only():
    node = make_node(
        transport="ws",
        remarks="",
        params={
            "path": "/ws",
            "host": "node.example.test",
            "sni": "node.example.test",
            "alpn": "h2,http/1.1",
            "udp": True,
            "skip-cert-verify": True,
            "obfs": "tls",
            "up": "50 Mbps",
            "down": "200 Mbps",
        },
    )
    entry, reason = build_clash_proxy(node)
    assert reason == ""
    assert entry is not None
    assert entry["name"] == "vless-192.0.2.10-443"
    assert entry["type"] == "vless"
    assert entry["server"] == "192.0.2.10"
    assert entry["port"] == 443
    assert entry["uuid"] == UUID
    assert entry["tls"] is True
    assert entry["servername"] == "node.example.test"
    assert entry["alpn"] == ["h2", "http/1.1"]
    assert entry["network"] == "ws"
    assert entry["ws-opts"] == {
        "path": "/ws",
        "headers": {"Host": "node.example.test"},
    }
    for name in ("udp", "skip-cert-verify", "obfs", "up", "down"):
        assert name not in entry


def test_build_clash_proxy_reality_without_keys_drops_tls_flag():
    node = make_node(security="reality", params={"sni": "node.example.test"})
    entry, _reason = build_clash_proxy(node)
    assert entry is not None
    assert "tls" not in entry
    assert entry["security"] == "reality"
    assert entry["servername"] == "node.example.test"

    node = make_node(
        security="reality",
        params={
            "sni": "node.example.test",
            "reality-opts": {"public-key": "pk", "short-id": "ab"},
        },
    )
    entry, _reason = build_clash_proxy(node)
    assert entry is not None
    assert entry["tls"] is True
    assert entry["reality-opts"] == {"public-key": "pk", "short-id": "ab"}
    assert "security" not in entry


def test_build_clash_proxy_grpc_service_name_and_remarks_name():
    node = make_node(transport="grpc", remarks="tokyo", params={"serviceName": "Gun"})
    entry, _reason = build_clash_proxy(node)
    assert entry is not None
    assert entry["name"] == "tokyo"
    assert entry["network"] == "grpc"
    assert entry["grpc-opts"] == {"grpc-service-name": "Gun"}


def test_build_clash_proxy_ss_uses_top_level_cipher_and_password():
    node = make_node(
        protocol="ss",
        secrets={"password": "pw", "cipher": "aes-128-gcm"},
    )
    entry, _reason = build_clash_proxy(node)
    assert entry is not None
    assert entry["type"] == "ss"
    assert entry["cipher"] == "aes-128-gcm"
    assert entry["password"] == "pw"
    assert "uuid" not in entry


def test_clash_round_trip_preserves_core_fields():
    source = Path(__file__).resolve().parents[1] / "input" / "sample-clash.yaml"
    parsed, issues = parse_clash_yaml(
        source.read_text(encoding="utf-8"), source_id="local:sample-clash.yaml"
    )
    assert not issues
    assert parsed

    nodes = []
    for index, item in enumerate(parsed):
        node = ProxyNode(
            item_id="round-{0}".format(index),
            kind="proxy_node",
            fingerprint="fp-round-{0}".format(index),
            fingerprint_version=1,
            protocol=item.protocol,
            server=item.server,
            port=item.port,
            transport=item.transport,
            security=item.security,
            params=dict(item.params),
            remarks=item.remarks,
            source_ids=["local:sample-clash.yaml"],
        )
        node.secrets.update(item.secrets)
        nodes.append(node)

    entries = []
    for node in nodes:
        entry, reason = build_clash_proxy(node)
        assert reason == ""
        assert entry is not None
        entries.append(entry)

    rebuilt, issues = parse_clash_yaml(
        build_clash_config(entries), source_id="roundtrip"
    )
    assert not issues
    assert len(rebuilt) == len(parsed)

    for original, again in zip(parsed, rebuilt):
        assert again.protocol == original.protocol
        assert again.server == original.server
        assert again.port == original.port
        assert again.transport == original.transport
        assert again.security == original.security
        assert again.remarks == original.remarks
        assert again.secrets == original.secrets
        for key in (
            "path",
            "host",
            "sni",
            "alpn",
            "flow",
            "client-fingerprint",
            "alter_id",
            "cipher",
            "serviceName",
            "reality-opts",
        ):
            if key in original.params:
                assert again.params.get(key) == original.params.get(key)


# --- Cloudflare exports --------------------------------------------------


def test_build_addapi_formats_lines_and_ipv6():
    text = build_addapi(
        [
            ("198.51.100.7", 443, "edge-a"),
            ("2001:db8::7", 8443, ""),
            ("198.51.100.8", 443, "has#hash"),
        ]
    )
    lines = text.splitlines()
    assert lines == [
        "198.51.100.7:443#edge-a",
        "[2001:db8::7]:8443",
        "198.51.100.8:443#hashash",
    ]
    assert text.endswith("\n")


def test_build_addapi_lines_match_documented_pattern():
    import re

    pattern = re.compile(r"^(ip|\[ipv6\]):\d{1,5}(#\S.*)?$")
    text = build_addapi([("198.51.100.7", 443, "edge-a"), ("2001:db8::7", 443, "")])
    for line in text.splitlines():
        candidate = line.replace("198.51.100.7", "ip").replace(
            "[2001:db8::7]", "[ipv6]"
        )
        assert pattern.match(candidate)


def test_build_addapi_empty_rows():
    assert build_addapi([]) == ""


# --- ADDAPI score-summary remark (v2rayN) ---------------------------------


def test_format_addapi_remark_default_template_matches_v2rayn_sample():
    remark = format_addapi_remark(
        speed_mb_s=4.6, purity=0.85, stability=0.72, country_code="SG"
    )
    assert remark == "4.6-0.85-0.72-新加坡"
    remark = format_addapi_remark(
        speed_mb_s=3.8, purity=0.80, stability=0.65, country_code="JP"
    )
    assert remark == "3.8-0.80-0.65-日本"


def test_format_addapi_remark_placeholder_template_is_customizable():
    remark = format_addapi_remark(
        speed_mb_s=4.6,
        purity=0.85,
        stability=0.72,
        country_code="SG",
        template="{country}:{speed}/{purity}/{stability}",
    )
    assert remark == "新加坡:4.6/0.85/0.72"
    remark = format_addapi_remark(
        speed_mb_s=4.6,
        purity=0.85,
        stability=0.72,
        country_code="SG",
        template="{speed}-{purity}-{country}",
    )
    assert remark == "4.6-0.85-新加坡"


def test_format_addapi_remark_missing_scores_use_dashes_and_unknown_country():
    remark = format_addapi_remark(speed_mb_s=8.0)
    assert remark == "8.0-------??"
    assert remark.startswith("8.0-")
    assert "--" in remark
    assert remark.endswith("??")
    # bare default template is the documented default
    assert DEFAULT_ADDAPI_REMARK_TEMPLATE == "speed-purity-stability-country"


def test_format_addapi_remark_normalizes_0_100_scores_to_0_1():
    remark = format_addapi_remark(
        speed_mb_s=11.59,
        purity=85.0,
        stability=72.0,
        country_code="jp",
    )
    assert remark == "11.6-0.85-0.72-日本"


def test_format_addapi_remark_speed_keeps_one_decimal():
    assert format_addapi_remark(speed_mb_s=11.59, purity=1, stability=1, country_code="US") == (
        "11.6-1.00-1.00-美国"
    )
    assert format_addapi_remark(speed_mb_s=0.05, purity=0, stability=0, country_code="") == (
        "0.1-0.00-0.00-??"
    )


def test_addapi_remark_from_ranked_uses_score_breakdown_then_risk_and_availability():
    ranked = make_ranked_endpoint(
        speed_mb_s=4.6,
        score_breakdown={"purity": 0.85, "stability": 0.72},
        country_code="SG",
        availability_rate=0.99,
    )
    assert addapi_remark_from_ranked(ranked) == "4.6-0.85-0.72-新加坡"

    ranked = make_ranked_endpoint(
        speed_mb_s=3.8,
        risk=20.0,
        availability_rate=0.65,
        country_code="JP",
    )
    assert addapi_remark_from_ranked(ranked) == "3.8-0.80-0.65-日本"

    ranked = make_ranked_endpoint(speed_mb_s=8.0)
    assert addapi_remark_from_ranked(ranked) == "8.0-------??"

    ranked = make_ranked_endpoint(speed_mb_s=8.0)
    assert addapi_remark_from_ranked(ranked, fallback="edge-a") == "8.0-------??"
    ranked = make_ranked_endpoint(speed_mb_s=8.0)
    assert addapi_remark_from_ranked(ranked, template="", fallback="edge-a") == "edge-a"


def test_build_export_addapi_lines_carry_score_summary_remark(tmp_path: Path):
    out = tmp_path / "export"
    outcome = build_export(
        out,
        make_report(),
        nodes=[],
        edges=[make_edge(address="104.17.29.227", port=8443)],
        proxies=[],
        endpoints=[
            make_ranked_endpoint(
                address="104.17.29.227",
                port=8443,
                speed_mb_s=4.6,
                score_breakdown={"purity": 0.85, "stability": 0.72},
                country_code="SG",
            )
        ],
        scoring_version="1",
    )
    assert outcome.status == "ok"
    text = (out / CF_ADDAPI_NAME).read_text(encoding="utf-8")
    assert text.splitlines() == ["104.17.29.227:8443#4.6-0.85-0.72-新加坡"]


def test_build_export_addapi_remark_template_is_configurable(tmp_path: Path):
    out = tmp_path / "export"
    build_export(
        out,
        make_report(),
        nodes=[],
        edges=[make_edge()],
        proxies=[],
        endpoints=[
            make_ranked_endpoint(
                speed_mb_s=4.6,
                score_breakdown={"purity": 0.85, "stability": 0.72},
                country_code="SG",
            )
        ],
        scoring_version="1",
        addapi_remark_template="{country}-{speed}",
    )
    text = (out / CF_ADDAPI_NAME).read_text(encoding="utf-8")
    assert text.splitlines() == ["198.51.100.7:443#新加坡-4.6"]


def test_build_addcsv_writes_full_header_and_formats():
    text = build_addcsv(
        [
            ("198.51.100.7", 443, "", True, "SJC", "US", "San Jose", 12.34, 5.678),
            ("198.51.100.8", 80, "origin", False, "", "", "", None, None),
        ]
    )
    lines = text.splitlines()
    assert lines[0] == ",".join(HEADER_FULL)
    assert len(HEADER_FULL) == 9
    assert lines[1] == "198.51.100.7,443,,true,SJC,US,San Jose,12.3,5.68"
    assert lines[2] == "198.51.100.8,80,origin,false,,,,,"
    assert text.startswith("IP地址,")


def test_build_addcsv_empty_rows_still_writes_header():
    text = build_addcsv([])
    assert text.splitlines() == [",".join(HEADER_FULL)]


# --- secret scanning -----------------------------------------------------


def test_scan_text_detects_credential_uri():
    errors = scan_text("proxy-raw.txt", "vless://uuid:pass@192.0.2.10:443#x")
    assert errors == ["credential uri in proxy-raw.txt"]
    assert scan_text("clean.txt", "vless://192.0.2.10:443#x") == []


def test_scan_text_matches_registered_secret_literal():
    register_secrets(["supersecretpw"])
    assert set(registered_secrets()) == {"supersecretpw"}
    assert scan_text("report.json", "pw=supersecretpw") == [
        "registered secret match in report.json"
    ]
    assert scan_text("report.json", "nothing here") == []


def test_scan_text_skips_short_and_numeric_secrets():
    register_secrets(["abc", "1234"])
    assert set(registered_secrets()) == {"1234"}
    assert scan_text("report.json", "abc 1234") == []


def test_scan_files_reads_existing_files_and_ignores_missing(tmp_path: Path):
    (tmp_path / "report.json").write_text("ok", encoding="utf-8")
    (tmp_path / "manifest.json").write_text("bad://x@y", encoding="utf-8")
    errors = scan_files(tmp_path, ["report.json", "manifest.json", "absent.txt"])
    assert errors == ["credential uri in manifest.json"]
    assert scan_files(tmp_path, ["absent.txt"]) == []


def test_scan_files_deduplicates_messages(tmp_path: Path):
    (tmp_path / "a.txt").write_text("bad://x@y", encoding="utf-8")
    (tmp_path / "b.txt").write_text("bad://x@y", encoding="utf-8")
    errors = scan_files(tmp_path, ["a.txt", "b.txt"])
    assert errors == ["credential uri in a.txt", "credential uri in b.txt"]


# --- report and manifest -------------------------------------------------


def test_report_bytes_match_json_encoding():
    report = make_report(note="东京")
    assert report_bytes(report) == json.dumps(
        report, ensure_ascii=False, indent=2, allow_nan=False
    ).encode("utf-8")


def test_build_manifest_sorts_files_and_keeps_order_keys():
    manifest = build_manifest(
        schema_version=1,
        run_id=RUN_ID,
        runner_id=RUNNER,
        profile="local",
        generated_at="2026-01-01T00:00:00Z",
        status="ok",
        scoring_version="1",
        validation={"ok": True, "errors": [], "files_checked": []},
        publishable=True,
        files=[
            {"name": "zeta.txt", "sha256": "a" * 64, "size_bytes": 1, "entry_count": 1},
            {"name": "alpha.txt", "sha256": "b" * 64, "size_bytes": 2, "entry_count": 0},
        ],
        counts={"files": 2},
    )
    names = [entry["name"] for entry in manifest["files"]]
    assert names == ["alpha.txt", "zeta.txt"]
    assert list(manifest["files"][0]) == [
        "name",
        "sha256",
        "size_bytes",
        "entry_count",
    ]
    assert manifest["scoring_version"] == "1"
    assert manifest["publishable"] is True


# --- build_export --------------------------------------------------------


def test_build_export_writes_six_files_and_valid_manifest(tmp_path: Path):
    out = tmp_path / "export"
    report = make_report()
    outcome = build_export(
        out,
        report,
        nodes=[make_node()],
        edges=[make_edge()],
        proxies=[make_ranked_proxy()],
        endpoints=[make_ranked_endpoint()],
        scoring_version="1",
    )
    assert outcome.status == "ok"
    assert outcome.validation is not None and outcome.validation.ok
    assert outcome.publishable is True
    assert sorted(path.name for path in out.iterdir()) == sorted(
        [
            PROXY_CLASH_NAME,
            PROXY_RAW_NAME,
            CF_ADDAPI_NAME,
            CF_ADDCSV_NAME,
            REPORT_NAME,
            MANIFEST_NAME,
        ]
    )
    assert outcome.counts["files"] == 6
    assert outcome.counts["proxies"] == 1
    assert outcome.counts["endpoints"] == 1
    assert outcome.counts["skipped"] == 0
    assert len(outcome.files) == 6

    manifest = json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest_names = [entry["name"] for entry in manifest["files"]]
    assert manifest_names == sorted(manifest_names)
    assert MANIFEST_NAME not in manifest_names
    assert manifest["counts"] == outcome.counts
    assert manifest["publishable"] is True
    assert manifest["generated_at"] == report["generated_at"]

    report_payload = (out / REPORT_NAME).read_bytes()
    entry = next(
        item for item in manifest["files"] if item["name"] == REPORT_NAME
    )
    assert entry["sha256"] == hashlib.sha256(report_payload).hexdigest()
    assert entry["size_bytes"] == len(report_payload)

    for entry in outcome.files:
        payload = (out / entry.name).read_bytes()
        assert entry.sha256 == hashlib.sha256(payload).hexdigest()
        assert entry.size_bytes == len(payload)


def test_build_export_records_skipped_reasons(tmp_path: Path):
    out = tmp_path / "export"
    broken = make_node(item_id="broken", secrets={}, remarks="broken")
    good = make_node(item_id="good")
    outcome = build_export(
        out,
        make_report(),
        nodes=[broken, good],
        edges=[],
        proxies=[
            make_ranked_proxy("broken", rank=1),
            make_ranked_proxy("good", rank=2),
        ],
        endpoints=[],
        scoring_version="1",
    )
    assert outcome.status == "ok"
    assert outcome.counts["proxies"] == 1
    assert outcome.counts["skipped"] == 1
    assert outcome.counts["skipped_missing_credentials"] == 1
    assert (out / PROXY_RAW_NAME).read_text(encoding="utf-8").count("\n") == 1


def test_build_export_without_content_is_not_publishable(tmp_path: Path):
    out = tmp_path / "export"
    outcome = build_export(
        out,
        make_report(),
        nodes=[],
        edges=[],
        proxies=[],
        endpoints=[],
        scoring_version="1",
    )
    assert outcome.status == "ok"
    assert outcome.publishable is False
    assert outcome.counts["proxies"] == 0
    assert outcome.counts["endpoints"] == 0
    assert (out / PROXY_CLASH_NAME).read_text(encoding="utf-8") == "proxies: []\n"
    assert (out / CF_ADDCSV_NAME).read_text(encoding="utf-8").splitlines() == [
        ",".join(HEADER_FULL)
    ]


def test_build_export_report_hides_credentials(tmp_path: Path):
    out = tmp_path / "export"
    node = make_node()
    node.secrets["uuid"] = UUID
    outcome = build_export(
        out,
        make_report(),
        nodes=[node],
        edges=[],
        proxies=[make_ranked_proxy()],
        endpoints=[],
        scoring_version="1",
    )
    assert outcome.status == "ok"
    text = (out / REPORT_NAME).read_text(encoding="utf-8")
    assert UUID not in text
    assert "secrets" not in yaml.safe_load(text)
