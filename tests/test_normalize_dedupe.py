from __future__ import annotations

from nodebench.core.schema import ParsedProxy
from nodebench.normalize.canonical import normalize_all, normalize_proxy
from nodebench.normalize.dedupe import dedupe

VLESS_UUID = "123e4567-e89b-12d3-a456-426614174000"


def parsed(**overrides):
    base = {
        "source_id": "src-a",
        "protocol": "vless",
        "server": "192.0.2.10",
        "port": 443,
        "transport": "tcp",
        "security": "tls",
        "params": {"host": "node.example.test"},
        "secrets": {"uuid": VLESS_UUID},
        "remarks": "",
    }
    base.update(overrides)
    return ParsedProxy(**base)


def node_for(source_id, **overrides):
    node, issue = normalize_proxy(parsed(**overrides), source_id)
    assert issue is None
    return node


def test_duplicate_across_sources_merged_in_order():
    first = node_for("src-a")
    second = node_for("src-b")
    proxies, endpoints = dedupe([first, second], [])
    assert endpoints == []
    assert len(proxies) == 1
    merged = proxies[0]
    assert merged.source_ids == ["src-a", "src-b"]
    assert merged.raw_refs == []


def test_remarks_taken_from_first_non_empty():
    silent = node_for("src-a")
    named = node_for("src-b", remarks="sample-node")
    proxies, _ = dedupe([silent, named], [])
    assert proxies[0].remarks == "sample-node"
    proxies, _ = dedupe([named, silent], [])
    assert proxies[0].remarks == "sample-node"


def test_raw_refs_union_preserved():
    first = node_for("src-a").model_copy(update={"raw_refs": ["line:1", "line:3"]})
    second = node_for("src-b").model_copy(update={"raw_refs": ["line:3", "line:4"]})
    proxies, _ = dedupe([first, second], [])
    assert proxies[0].raw_refs == ["line:1", "line:3", "line:4"]


def test_different_transport_kept_separate():
    tcp = node_for("src-a", transport="tcp")
    ws = node_for("src-a", transport="ws")
    proxies, _ = dedupe([tcp, ws], [])
    assert len(proxies) == 2
    assert sorted(node.transport for node in proxies) == ["tcp", "ws"]


def test_endpoints_merged_by_fingerprint():
    from nodebench.core.schema import ParsedEndpoint
    from nodebench.normalize.canonical import normalize_endpoint

    first, issue = normalize_endpoint(
        ParsedEndpoint(
            source_id="src-a",
            address="192.0.2.40",
            port=443,
            tls=True,
            params={"datacenter": "sample-dc"},
            remarks="",
        ),
        "src-a",
    )
    assert issue is None
    second, issue = normalize_endpoint(
        ParsedEndpoint(
            source_id="src-b",
            address="192.0.2.40",
            port=443,
            tls=True,
            params={},
            remarks="sample-edge",
        ),
        "src-b",
    )
    assert issue is None
    proxies, endpoints = dedupe([], [first, second])
    assert proxies == []
    assert len(endpoints) == 1
    merged = endpoints[0]
    assert merged.source_ids == ["src-a", "src-b"]
    assert merged.remarks == "sample-edge"


def test_output_sorted_by_fingerprint():
    nodes = [
        node_for("src-a", server="192.0.2.{0}".format(i)) for i in range(10, 20)
    ]
    proxies, _ = dedupe(nodes, [])
    fingerprints = [node.fingerprint for node in proxies]
    assert fingerprints == sorted(fingerprints)


def test_inputs_not_mutated():
    first = node_for("src-a")
    second = node_for("src-b", remarks="sample-node")
    proxies, _ = dedupe([first, second], [])
    assert len(proxies) == 1
    assert first.source_ids == ["src-a"]
    assert second.source_ids == ["src-b"]
    assert first.remarks == ""
    assert second.remarks == "sample-node"
    assert proxies[0] is not first
    assert proxies[0] is not second


def test_mixed_kinds_returned_separately():
    from nodebench.core.schema import ParsedEndpoint
    from nodebench.normalize.canonical import normalize_endpoint

    proxy_node = node_for("src-a")
    edge, issue = normalize_endpoint(
        ParsedEndpoint(
            source_id="src-b",
            address="198.51.100.42",
            port=2053,
            tls=True,
        ),
        "src-b",
    )
    assert issue is None
    proxies, endpoints = dedupe([proxy_node], [edge])
    assert len(proxies) == 1
    assert len(endpoints) == 1
    assert proxies[0].kind == "proxy_node"
    assert endpoints[0].kind == "edge_endpoint"


def test_normalize_all_preserves_order_and_appends_issues():
    from nodebench.parsers.common import make_issue

    good_a = parsed(server="192.0.2.10")
    good_b = parsed(server="198.51.100.10")
    bad = parsed(server="")
    issue_before = make_issue("src-a", "invalid_uri", "kept")
    nodes, edges, issues = normalize_all([good_a, good_b, bad], [], [issue_before])
    assert [node.server for node in nodes] == ["192.0.2.10", "198.51.100.10"]
    assert edges == []
    assert issues[0] is issue_before
    assert [issue.code for issue in issues] == ["invalid_uri", "missing_server"]
    assert issues[1].source_id == "src-a"


def test_normalize_all_uses_parsed_source_id():
    bad = parsed(source_id="src-own", port=0)
    _, _, issues = normalize_all([bad], [], [])
    assert issues[0].code == "invalid_port"
    assert issues[0].source_id == "src-own"
