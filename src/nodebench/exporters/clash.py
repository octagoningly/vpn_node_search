from __future__ import annotations

from typing import Any

import yaml

from nodebench.core.schema import ProxyNode
from nodebench.core.fields import default_transport, sanitize_token

PROXY_CLASH_NAME = "proxy-clash.yaml"

CLASH_SCHEMES = frozenset({"vless", "vmess", "trojan", "ss", "hysteria2", "tuic"})

REASON_MISSING_CREDENTIALS = "missing_credentials"
REASON_UNSUPPORTED_PROTOCOL = "unsupported_protocol"


def _first(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return value
    return None


def clash_skip_reason(node: ProxyNode) -> str:
    protocol = (node.protocol or "").strip().lower()
    if protocol not in CLASH_SCHEMES:
        return REASON_UNSUPPORTED_PROTOCOL
    secrets = node.secrets or {}
    if protocol in ("vless", "vmess"):
        if not _first(secrets.get("uuid")):
            return REASON_MISSING_CREDENTIALS
    elif protocol == "trojan":
        if not _first(secrets.get("password")):
            return REASON_MISSING_CREDENTIALS
    elif protocol == "hysteria2":
        if not _first(secrets.get("password")):
            return REASON_MISSING_CREDENTIALS
    elif protocol == "tuic":
        if not _first(secrets.get("uuid")) or not _first(secrets.get("password")):
            return REASON_MISSING_CREDENTIALS
    elif protocol == "ss":
        if not _first(secrets.get("password")):
            return REASON_MISSING_CREDENTIALS
        if not _ss_cipher(node):
            return REASON_MISSING_CREDENTIALS
    return ""


def _ss_cipher(node: ProxyNode) -> str:
    text = _first(
        node.secrets.get("cipher"),
        node.params.get("method"),
        node.params.get("cipher"),
    )
    return str(text) if text is not None else ""


def proxy_name(node: ProxyNode) -> str:
    remarks = sanitize_token(node.remarks, limit=256)
    if remarks:
        return remarks
    return "{0}-{1}-{2}".format(
        (node.protocol or "").strip().lower(), node.server, int(node.port)
    )


def _reality_keys(params: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    opts = params.get("reality-opts")
    if isinstance(opts, dict):
        public_key = _first(
            opts.get("public-key"),
            opts.get("publicKey"),
            opts.get("public_key"),
        )
        short_id = _first(
            opts.get("short-id"),
            opts.get("shortId"),
            opts.get("short_id"),
        )
    else:
        public_key = None
        short_id = None
    public_key = _first(
        public_key, params.get("reality_public_key"), params.get("reality-public-key")
    )
    short_id = _first(
        short_id, params.get("reality_short_id"), params.get("reality-short-id")
    )
    if public_key is not None:
        found["public-key"] = str(public_key)
    if short_id is not None:
        found["short-id"] = str(short_id)
    return found


def _alpn_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    text = str(value or "")
    if not text:
        return []
    return [item for item in text.split(",") if item]


def build_clash_proxy(node: ProxyNode) -> tuple[dict[str, Any] | None, str]:
    reason = clash_skip_reason(node)
    if reason:
        return None, reason
    protocol = (node.protocol or "").strip().lower()
    params = node.params or {}
    secrets = node.secrets or {}
    transport = (node.transport or default_transport(protocol)).strip() or default_transport(protocol)
    security = (node.security or "none").strip().lower()
    entry: dict[str, Any] = {
        "name": proxy_name(node),
        "type": protocol,
        "server": node.server,
        "port": int(node.port),
    }
    if protocol in ("vless", "vmess"):
        entry["uuid"] = str(secrets.get("uuid") or "")
    elif protocol in ("trojan", "hysteria2", "tuic"):
        entry["password"] = str(secrets.get("password") or "")
        if protocol == "tuic":
            entry["uuid"] = str(secrets.get("uuid") or "")
    elif protocol == "ss":
        entry["password"] = str(secrets.get("password") or "")
        entry["cipher"] = _ss_cipher(node)
    cipher = params.get("cipher")
    if protocol != "ss" and str(cipher or ""):
        entry["cipher"] = str(cipher)
    if params.get("alter_id") not in (None, "", 0, "0"):
        try:
            entry["alterId"] = int(str(params["alter_id"]).strip())
        except ValueError:
            entry["alterId"] = params["alter_id"]
    if str(params.get("flow") or ""):
        entry["flow"] = str(params["flow"])
    if transport != default_transport(protocol):
        entry["network"] = transport
    path = _first(params.get("path"))
    host = _first(params.get("host"))
    if path is not None or host is not None:
        ws_opts: dict[str, Any] = {}
        if path is not None:
            ws_opts["path"] = str(path)
        if host is not None:
            ws_opts["headers"] = {"Host": str(host)}
        entry["ws-opts"] = ws_opts
    service_name = _first(params.get("serviceName"), params.get("service-name"))
    if service_name is not None:
        entry["grpc-opts"] = {"grpc-service-name": str(service_name)}
    sni = _first(params.get("sni"), params.get("servername"), params.get("peer"))
    if security in ("tls", "reality"):
        entry["tls"] = True
    else:
        entry["tls"] = False
    if security == "reality":
        reality = _reality_keys(params)
        if reality:
            entry["reality-opts"] = reality
        else:
            entry.pop("tls", None)
            entry["security"] = "reality"
    if sni is not None:
        entry["servername"] = str(sni)
    fingerprint = _first(params.get("client-fingerprint"), params.get("fp"))
    if fingerprint is not None:
        entry["client-fingerprint"] = str(fingerprint)
    alpn = _alpn_list(params.get("alpn"))
    if alpn:
        entry["alpn"] = alpn
    plugin = params.get("plugin")
    if isinstance(plugin, dict) and plugin.get("name"):
        entry["plugin"] = str(plugin.get("name"))
        opts = plugin.get("opts")
        if isinstance(opts, dict):
            entry["plugin-opts"] = dict(opts)
        else:
            entry["plugin-opts"] = {}
    elif str(plugin or ""):
        entry["plugin"] = str(plugin)
    return entry, ""


def build_clash_config(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "proxies: []\n"
    return yaml.safe_dump(
        {"proxies": entries},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=4096,
    )
