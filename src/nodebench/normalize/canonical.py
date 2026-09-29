from __future__ import annotations

from nodebench.core.fields import (
    INVALID_PORT,
    MISSING_SERVER,
    UNSUPPORTED_PROTOCOL,
    make_issue,
    normalize_server,
    parse_port,
)
from nodebench.core.schema import (
    FINGERPRINT_VERSION,
    EdgeEndpoint,
    ProtocolSupport,
    ProxyNode,
)
from nodebench.normalize.fingerprint import fingerprint_endpoint, fingerprint_proxy


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


def _mapping(value):
    if isinstance(value, dict):
        return dict(value)
    return {}


def normalize_proxy(parsed, source_id=""):
    protocol = str(_field(parsed, "protocol", "") or "").strip().lower()
    server = normalize_server(_field(parsed, "server", ""))
    port = parse_port(_field(parsed, "port"))
    transport = str(_field(parsed, "transport", "") or "").strip().lower()
    security = str(_field(parsed, "security", "") or "").strip().lower()
    owner = source_id or _field(parsed, "source_id", "")
    if not server:
        return None, make_issue(owner, MISSING_SERVER, "missing server")
    if port is None:
        return None, make_issue(owner, INVALID_PORT, "invalid port")
    if not protocol or not transport or not security:
        return None, make_issue(
            owner, UNSUPPORTED_PROTOCOL, "missing protocol fields"
        )
    params = _mapping(_field(parsed, "params", {}))
    secrets = _mapping(_field(parsed, "secrets", {}))
    remarks = str(_field(parsed, "remarks", "") or "")
    support = _field(parsed, "protocol_support", None)
    try:
        protocol_support = ProtocolSupport(support) if support is not None else ProtocolSupport.PARSE_ONLY
    except ValueError:
        protocol_support = ProtocolSupport.PARSE_ONLY
    fingerprint = fingerprint_proxy(
        {
            "protocol": protocol,
            "server": server,
            "port": port,
            "transport": transport,
            "security": security,
            "params": params,
            "secrets": secrets,
        }
    )
    node = ProxyNode(
        item_id=fingerprint,
        kind="proxy_node",
        fingerprint=fingerprint,
        fingerprint_version=FINGERPRINT_VERSION,
        protocol=protocol,
        server=server,
        port=port,
        transport=transport,
        security=security,
        params=params,
        secrets=secrets,
        remarks=remarks,
        source_ids=[owner] if owner else [],
        raw_refs=[],
        protocol_support=protocol_support,
    )
    return node, None


def normalize_endpoint(parsed, source_id=""):
    address = normalize_server(_field(parsed, "address", ""))
    port = parse_port(_field(parsed, "port"))
    owner = source_id or _field(parsed, "source_id", "")
    if not address:
        return None, make_issue(owner, MISSING_SERVER, "missing address")
    if port is None:
        return None, make_issue(owner, INVALID_PORT, "invalid port")
    target_host = normalize_server(_field(parsed, "target_host", ""))
    tls = bool(_field(parsed, "tls", False))
    params = _mapping(_field(parsed, "params", {}))
    remarks = str(_field(parsed, "remarks", "") or "")
    fingerprint = fingerprint_endpoint(
        {
            "address": address,
            "port": port,
            "target_host": target_host,
            "tls": tls,
        }
    )
    node = EdgeEndpoint(
        item_id=fingerprint,
        kind="edge_endpoint",
        fingerprint=fingerprint,
        fingerprint_version=FINGERPRINT_VERSION,
        address=address,
        port=port,
        target_host=target_host,
        tls=tls,
        params=params,
        remarks=remarks,
        source_ids=[owner] if owner else [],
        raw_refs=[],
    )
    return node, None


def normalize_all(proxies, endpoints, issues):
    collected = list(issues)
    nodes = []
    edges = []
    for parsed in proxies:
        owner = _field(parsed, "source_id", "")
        node, issue = normalize_proxy(parsed, owner)
        if node is not None:
            nodes.append(node)
        if issue is not None:
            collected.append(issue)
    for parsed in endpoints:
        owner = _field(parsed, "source_id", "")
        node, issue = normalize_endpoint(parsed, owner)
        if node is not None:
            edges.append(node)
        if issue is not None:
            collected.append(issue)
    return nodes, edges, collected
