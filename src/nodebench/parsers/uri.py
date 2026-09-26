from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, unquote

from nodebench.core.schema import ParsedProxy
from nodebench.parsers.common import (
    INVALID_PORT,
    INVALID_URI,
    MISSING_CREDENTIALS,
    MISSING_SERVER,
    UNSUPPORTED_PROTOCOL,
    b64decode_flexible,
    coerce_security,
    coerce_transport,
    fold_defaults,
    make_issue,
    normalize_server,
    parse_port,
    redact_uri_ref,
    sanitize_token,
)

SUPPORTED_SCHEMES = frozenset({"vless", "vmess", "trojan", "ss", "hysteria2", "tuic"})

_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*")

_CREDENTIAL_KEYS = ("token", "auth", "password", "uuid")
_TRANSPORT_KEYS = ("type", "net", "network")
_SECURITY_KEYS = ("security", "tls")

_PARAM_RENAMES = {
    "sni": "sni",
    "peer": "sni",
    "servername": "sni",
    "scy": "cipher",
    "aid": "alter_id",
    "alterid": "alter_id",
    "pbk": "reality_public_key",
    "sid": "reality_short_id",
    "fp": "fp",
    "path": "path",
    "host": "host",
    "servicename": "serviceName",
    "alpn": "alpn",
    "flow": "flow",
    "headertype": "headerType",
}


def _split_fragment(text):
    if "#" in text:
        head, _, fragment = text.partition("#")
        return head, sanitize_token(unquote(fragment), limit=256)
    return text, ""


def _split_query(authority):
    if "?" in authority:
        head, _, query = authority.partition("?")
        return head, query
    return authority, ""


def _split_hostport(hostport):
    if hostport.startswith("["):
        close = hostport.find("]")
        if close == -1:
            return None, None, INVALID_URI
        host = hostport[1:close]
        tail = hostport[close + 1 :]
        if tail == "":
            return host, None, None
        if tail.startswith(":"):
            return host, tail[1:], None
        return None, None, INVALID_URI
    colons = hostport.count(":")
    if colons == 0:
        return hostport, None, INVALID_PORT
    if colons == 1:
        host, _, port_text = hostport.partition(":")
        return host, port_text, None
    return None, None, INVALID_URI


def _pop_secrets(query):
    secrets = {}
    for key in list(query):
        lowered = key.lower()
        if lowered in _CREDENTIAL_KEYS:
            secrets[lowered] = query.pop(key)
    return secrets


def _promote(query):
    transport_value = None
    security_value = None
    for key in list(query):
        lowered = key.lower()
        if lowered in _TRANSPORT_KEYS:
            if transport_value is None:
                transport_value = query.pop(key)
            else:
                query.pop(key)
        elif lowered in _SECURITY_KEYS:
            if security_value is None:
                security_value = query.pop(key)
            else:
                query.pop(key)
    return transport_value, security_value


def _rename_params(query):
    params = {}
    for key, value in query.items():
        target = _PARAM_RENAMES.get(key.lower(), key)
        if target == "alter_id":
            try:
                params[target] = int(str(value).strip())
            except ValueError:
                params[target] = value
        else:
            params[target] = value
    return params


def _parse_plugin(value):
    segments = [segment for segment in str(value).split(";") if segment]
    name = ""
    opts = {}
    for index, segment in enumerate(segments):
        if "=" in segment:
            key, _, opt_value = segment.partition("=")
            opts[key.strip()] = opt_value
        elif index == 0 and not name:
            name = segment
    return {"name": name, "opts": opts}


def _vmess_from_json(payload, source_id, raw_ref, fallback_remarks):
    decoded = b64decode_flexible(payload)
    if decoded is None:
        return None
    try:
        data = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    server = normalize_server(data.get("add"))
    if not server:
        return None, make_issue(source_id, MISSING_SERVER, "missing server", raw_ref)
    port = parse_port(data.get("port"))
    if port is None:
        return None, make_issue(source_id, INVALID_PORT, "invalid port", raw_ref)
    uuid_value = str(data.get("id") or "").strip()
    if not uuid_value:
        return None, make_issue(source_id, MISSING_CREDENTIALS, "missing credentials", raw_ref)
    remarks = sanitize_token(data.get("ps"), limit=256) or fallback_remarks
    params = {}
    if data.get("scy"):
        params["cipher"] = str(data.get("scy"))
    if data.get("type"):
        params["headerType"] = str(data.get("type"))
    for key in ("host", "path", "sni", "alpn", "fp"):
        if data.get(key):
            params[key] = str(data.get(key))
    if data.get("aid") is not None:
        try:
            params["alter_id"] = int(str(data.get("aid")).strip())
        except ValueError:
            params["alter_id"] = data.get("aid")
    fold_defaults(params)
    node = ParsedProxy(
        source_id=source_id,
        protocol="vmess",
        server=server,
        port=port,
        transport=coerce_transport(data.get("net"), "vmess"),
        security=coerce_security(data.get("tls"), "vmess"),
        params=params,
        secrets={"uuid": uuid_value},
        remarks=remarks,
    )
    return node, None


def _split_authority(authority):
    userinfo, at, hostport = authority.rpartition("@")
    if at == "":
        return "", hostport
    return userinfo, hostport


def parse_uri(line, source_id):
    text = "" if line is None else str(line).strip()
    raw_ref = redact_uri_ref(text)
    body, remarks = _split_fragment(text)
    body, query_text = _split_query(body)
    if "://" not in body:
        return None, make_issue(source_id, INVALID_URI, "missing scheme separator", raw_ref)
    scheme_text, sep, authority = body.partition("://")
    if sep == "" or _SCHEME_RE.fullmatch(scheme_text) is None:
        return None, make_issue(source_id, INVALID_URI, "malformed uri", raw_ref)
    scheme = scheme_text.lower()
    if scheme == "hy2":
        scheme = "hysteria2"
    if scheme not in SUPPORTED_SCHEMES:
        safe_scheme = sanitize_token(scheme_text, limit=32)
        return None, make_issue(
            source_id,
            UNSUPPORTED_PROTOCOL,
            "unsupported scheme: {0}".format(safe_scheme),
            raw_ref,
        )
    query = {}
    for key, value in parse_qsl(query_text, keep_blank_values=True):
        query[key] = value
    secrets = _pop_secrets(query)
    transport_value, security_value = _promote(query)
    params = _rename_params(query)

    if scheme == "vmess":
        outcome = _vmess_from_json(authority, source_id, raw_ref, remarks)
        if outcome is not None:
            parsed, issue = outcome
            if parsed is not None:
                return parsed, None
            return None, issue
        if "@" not in authority:
            return None, make_issue(
                source_id,
                UNSUPPORTED_PROTOCOL,
                "unsupported vmess payload",
                raw_ref,
            )

    hostport = authority
    userinfo = ""
    ss_method = None
    if scheme == "ss":
        if "@" in authority:
            userinfo, hostport = _split_authority(authority)
        else:
            decoded = b64decode_flexible(authority)
            inner = None
            if decoded is not None:
                try:
                    inner = decoded.decode("utf-8")
                except UnicodeDecodeError:
                    inner = None
            if inner is None or "@" not in inner:
                return None, make_issue(
                    source_id,
                    MISSING_CREDENTIALS,
                    "unable to decode ss credentials",
                    raw_ref,
                )
            creds, _, hostport = inner.rpartition("@")
            ss_method, sep, password = creds.partition(":")
            if sep == "" or not ss_method or not password:
                return None, make_issue(
                    source_id,
                    MISSING_CREDENTIALS,
                    "missing ss credentials",
                    raw_ref,
                )
            secrets["password"] = password
    elif "@" in authority:
        userinfo, hostport = _split_authority(authority)

    host, port_text, host_error = _split_hostport(hostport)
    if host_error is not None:
        return None, make_issue(
            source_id,
            host_error,
            "invalid authority" if host_error == INVALID_URI else "invalid port",
            raw_ref,
        )
    server = normalize_server(host)
    if not server:
        return None, make_issue(source_id, MISSING_SERVER, "missing server", raw_ref)
    port = parse_port(port_text) if port_text is not None else None
    if port is None:
        return None, make_issue(source_id, INVALID_PORT, "invalid port", raw_ref)

    if scheme == "ss":
        if ss_method is None:
            decoded = b64decode_flexible(unquote(userinfo))
            inner = None
            if decoded is not None:
                try:
                    inner = decoded.decode("utf-8")
                except UnicodeDecodeError:
                    inner = None
            if inner is None or ":" not in inner:
                inner = unquote(userinfo)
            ss_method, sep, password = inner.partition(":")
            if sep == "" or not ss_method or not password:
                return None, make_issue(
                    source_id,
                    MISSING_CREDENTIALS,
                    "missing ss credentials",
                    raw_ref,
                )
            secrets["password"] = password
        params["method"] = ss_method
    elif scheme in ("vless", "vmess"):
        uuid_value = unquote(userinfo).strip() if userinfo else ""
        if not uuid_value:
            uuid_value = str(secrets.get("uuid") or "").strip()
        if not uuid_value:
            return None, make_issue(
                source_id, MISSING_CREDENTIALS, "missing uuid", raw_ref
            )
        secrets["uuid"] = uuid_value
    elif scheme == "trojan":
        password = unquote(userinfo).strip() if userinfo else ""
        if not password:
            password = str(secrets.get("password") or "").strip()
        if not password:
            return None, make_issue(
                source_id, MISSING_CREDENTIALS, "missing password", raw_ref
            )
        secrets["password"] = password
    elif scheme == "hysteria2":
        if userinfo and unquote(userinfo).strip():
            secrets["password"] = unquote(userinfo).strip()
        elif "auth" in secrets:
            secrets["password"] = secrets.pop("auth")
        elif "token" in secrets:
            pass
        else:
            return None, make_issue(
                source_id, MISSING_CREDENTIALS, "missing auth token", raw_ref
            )
    elif scheme == "tuic":
        credential = unquote(userinfo).strip() if userinfo else ""
        uuid_value, sep, password = credential.partition(":")
        if sep == "" or not uuid_value or not password:
            return None, make_issue(
                source_id, MISSING_CREDENTIALS, "missing tuic credentials", raw_ref
            )
        secrets["uuid"] = uuid_value
        secrets["password"] = password

    if "plugin" in params and scheme == "ss":
        params["plugin"] = _parse_plugin(params["plugin"])

    transport = coerce_transport(transport_value, scheme)
    security = coerce_security(security_value, scheme)
    fold_defaults(params)
    node = ParsedProxy(
        source_id=source_id,
        protocol=scheme,
        server=server,
        port=port,
        transport=transport,
        security=security,
        params=params,
        secrets=secrets,
        remarks=remarks,
    )
    return node, None
