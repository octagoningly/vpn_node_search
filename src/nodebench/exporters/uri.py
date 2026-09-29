from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote, urlencode

from nodebench.core.schema import ProxyNode
from nodebench.core.fields import default_transport, sanitize_token

URI_SCHEMES = frozenset({"vless", "vmess", "trojan", "ss", "hysteria2", "tuic"})

REASON_MISSING_CREDENTIALS = "missing_credentials"
REASON_UNSUPPORTED_PROTOCOL = "unsupported_protocol"

_CREDENTIAL_FIELDS = {
    "vless": ("uuid",),
    "vmess": ("uuid",),
    "trojan": ("password",),
    "hysteria2": ("password",),
    "tuic": ("uuid", "password"),
    "ss": ("password",),
}

_FIXED_QUERY_ORDER = (
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
)

_HANDLED_PARAMS = frozenset(
    {
        "sni",
        "alpn",
        "path",
        "host",
        "flow",
        "fp",
        "client-fingerprint",
        "alter_id",
        "cipher",
        "serviceName",
        "headerType",
        "plugin",
        "reality-opts",
        "reality_public_key",
        "reality_short_id",
    }
)

_SWALLOWED_QUERY_KEYS = frozenset(
    {
        "type",
        "network",
        "net",
        "security",
        "tls",
        "uuid",
        "password",
        "token",
        "auth",
        "encryption",
    }
)


def _ss_password(node: ProxyNode) -> str:
    return str(node.secrets.get("password") or "").strip()


def _ss_method(node: ProxyNode) -> str:
    for source in (
        node.params.get("method"),
        node.secrets.get("cipher"),
        node.params.get("cipher"),
    ):
        text = str(source or "").strip()
        if text:
            return text
    return ""


def skip_reason(node: ProxyNode) -> str:
    protocol = (node.protocol or "").strip().lower()
    if protocol not in URI_SCHEMES:
        return REASON_UNSUPPORTED_PROTOCOL
    if protocol == "ss":
        if not _ss_password(node) or not _ss_method(node):
            return REASON_MISSING_CREDENTIALS
        return ""
    for field in _CREDENTIAL_FIELDS.get(protocol, ()):
        if not str(node.secrets.get(field) or "").strip():
            return REASON_MISSING_CREDENTIALS
    return ""


def _userinfo(node: ProxyNode, protocol: str) -> str:
    if protocol == "ss":
        raw = "{0}:{1}".format(_ss_method(node), _ss_password(node))
        return quote(base64.b64encode(raw.encode("utf-8")).decode("ascii"), safe="")
    if protocol == "tuic":
        raw = "{0}:{1}".format(
            str(node.secrets.get("uuid") or "").strip(),
            str(node.secrets.get("password") or "").strip(),
        )
        return quote(raw, safe="")
    field = _CREDENTIAL_FIELDS[protocol][0]
    return quote(str(node.secrets.get(field) or "").strip(), safe="")


def _authority(node: ProxyNode) -> str:
    host = "[{0}]".format(node.server) if ":" in node.server else node.server
    return "{0}:{1}".format(host, int(node.port))


def _reality_value(params: dict[str, Any], names: tuple[str, ...]) -> str:
    opts = params.get("reality-opts")
    if isinstance(opts, dict):
        for name in names:
            value = opts.get(name)
            if value is not None and str(value).strip():
                return str(value).strip()
    for name in names:
        if name in params:
            value = params.get(name)
            if value is not None and str(value).strip():
                return str(value).strip()
    return ""


def _query_pairs(node: ProxyNode) -> list[tuple[str, str]]:
    params = node.params or {}
    pairs: dict[str, str] = {"type": node.transport, "security": node.security}
    if str(params.get("sni") or ""):
        pairs["sni"] = str(params["sni"])
    if str(params.get("alpn") or ""):
        pairs["alpn"] = str(params["alpn"])
    if str(params.get("path") or ""):
        pairs["path"] = str(params["path"])
    if str(params.get("host") or ""):
        pairs["host"] = str(params["host"])
    if str(params.get("flow") or ""):
        pairs["flow"] = str(params["flow"])
    fingerprint = params.get("fp") or params.get("client-fingerprint")
    if str(fingerprint or ""):
        pairs["fp"] = str(fingerprint)
    if params.get("alter_id") not in (None, "", 0, "0"):
        pairs["aid"] = str(params["alter_id"])
    public_key = _reality_value(
        params, ("reality_public_key", "publicKey", "public-key", "public_key")
    )
    if public_key:
        pairs["pbk"] = public_key
    short_id = _reality_value(
        params, ("reality_short_id", "shortId", "short-id", "short_id")
    )
    if short_id:
        pairs["sid"] = short_id
    cipher = params.get("cipher") or params.get("method")
    if str(cipher or ""):
        pairs["scy"] = str(cipher)
    if str(params.get("serviceName") or ""):
        pairs["serviceName"] = str(params["serviceName"])
    if str(params.get("headerType") or ""):
        pairs["headerType"] = str(params["headerType"])
    plugin = params.get("plugin")
    if isinstance(plugin, dict):
        name = str(plugin.get("name") or "")
        opts = plugin.get("opts")
        segments = [name] if name else []
        if isinstance(opts, dict):
            for key in sorted(opts):
                segments.append("{0}={1}".format(key, opts[key]))
        if segments:
            pairs["plugin"] = ";".join(segments)
    elif str(plugin or ""):
        pairs["plugin"] = str(plugin)
    for key in sorted(params):
        lowered = key.lower()
        if lowered in _SWALLOWED_QUERY_KEYS or key in _HANDLED_PARAMS:
            continue
        value = params.get(key)
        if isinstance(value, (dict, list)) or value is None or str(value) == "":
            continue
        pairs[key] = str(value)
    ordered = [("type", pairs.pop("type")), ("security", pairs.pop("security"))]
    for key in _FIXED_QUERY_ORDER:
        if key in pairs:
            ordered.append((key, pairs.pop(key)))
    for key in sorted(pairs):
        ordered.append((key, pairs.pop(key)))
    return ordered


def build_proxy_uri(node: ProxyNode) -> tuple[str | None, str]:
    protocol = (node.protocol or "").strip().lower()
    reason = skip_reason(node)
    if reason:
        return None, reason
    remarks = sanitize_token(node.remarks, limit=256)
    if protocol == "vmess":
        return _vmess_uri(node, remarks), ""
    query = urlencode(_query_pairs(node), doseq=True)
    body = "{0}://{1}@{2}".format(protocol, _userinfo(node, protocol), _authority(node))
    if query:
        body = "{0}?{1}".format(body, query)
    if remarks:
        body = "{0}#{1}".format(body, quote(remarks, safe=""))
    return body, ""


def _vmess_uri(node: ProxyNode, remarks: str) -> str:
    payload: dict[str, Any] = {
        "v": "2",
        "add": node.server,
        "port": str(int(node.port)),
        "id": str(node.secrets.get("uuid") or "").strip(),
        "net": node.transport or default_transport("vmess"),
    }
    if remarks:
        payload["ps"] = remarks
    security = (node.security or "none").strip().lower()
    payload["tls"] = "" if security == "none" else security
    params = node.params or {}
    cipher = params.get("cipher") or params.get("method")
    if str(cipher or ""):
        payload["scy"] = str(cipher)
    if str(params.get("headerType") or ""):
        payload["type"] = str(params["headerType"])
    if params.get("alter_id") not in (None, "", 0, "0"):
        payload["aid"] = str(params["alter_id"])
    fingerprint = params.get("fp") or params.get("client-fingerprint")
    for key, value in (
        ("host", params.get("host")),
        ("path", params.get("path")),
        ("sni", params.get("sni")),
        ("alpn", params.get("alpn")),
        ("fp", fingerprint),
    ):
        if str(value or ""):
            payload[key] = str(value)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    token = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return "vmess://{0}".format(token)


def build_raw_text(uris: list[str]) -> str:
    if not uris:
        return ""
    return "".join("{0}\n".format(uri) for uri in uris)
