from __future__ import annotations

import yaml

from nodebench.core.schema import ParsedProxy
from nodebench.parsers.common import (
    FILE_TOO_LARGE,
    INVALID_PORT,
    INVALID_YAML,
    MISSING_CREDENTIALS,
    MISSING_PROXIES,
    MISSING_SERVER,
    PROXY_LIMIT_EXCEEDED,
    UNSUPPORTED_PROTOCOL,
    coerce_security,
    coerce_transport,
    fold_defaults,
    make_issue,
    normalize_server,
    parse_port,
    sanitize_token,
)

MAX_YAML_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 20
MAX_PROXIES = 2000

_KNOWN_KEYS = frozenset(
    {
        "type",
        "server",
        "port",
        "name",
        "network",
        "uuid",
        "password",
        "cipher",
        "alterId",
        "alterid",
        "tls",
        "security",
        "sni",
        "servername",
        "ws-opts",
        "grpc-opts",
        "http-opts",
        "h2-opts",
        "reality-opts",
        "alpn",
        "plugin",
        "plugin-opts",
        "auth",
        "token",
        "udp",
        "skip-cert-verify",
        "client-fingerprint",
        "flow",
        "fingerprint",
        "obfs",
        "obfs-param",
        "obfs-password",
        "peer",
        "up",
        "down",
        "congestion-controller",
        "congestion_control",
        "udp-relay-mode",
        "udp_relay_mode",
    }
)

_CREDENTIAL_KEYS = ("uuid", "password", "auth", "token")


def _depth(value, level=0):
    if level > MAX_DEPTH:
        return MAX_DEPTH + 1
    if isinstance(value, dict):
        children = [_depth(child, level + 1) for child in value.values()]
        return 1 + (max(children) if children else 0)
    if isinstance(value, list):
        children = [_depth(child, level + 1) for child in value]
        return 1 + (max(children) if children else 0)
    return 0


def _is_reality(entry):
    opts = entry.get("reality-opts")
    return isinstance(opts, dict) and bool(opts)


def _security_value(entry, protocol):
    if _is_reality(entry):
        return "reality"
    explicit = entry.get("tls")
    if explicit is None:
        explicit = entry.get("security")
    return coerce_security(explicit, protocol)


def _collect_creds(entry, protocol):
    secrets = {}
    missing = False
    for key in _CREDENTIAL_KEYS:
        value = entry.get(key)
        if value is None:
            continue
        text = str(value)
        if text.strip():
            secrets[key] = text
    obfs_password = entry.get("obfs-password")
    if obfs_password is None:
        obfs_password = entry.get("obfs_password")
    if obfs_password is not None and str(obfs_password).strip():
        secrets["obfs_password"] = str(obfs_password)
    if protocol in ("vless", "vmess"):
        missing = not secrets.get("uuid")
    elif protocol in ("trojan", "ss"):
        missing = not secrets.get("password")
    elif protocol == "hysteria2":
        if not secrets.get("password") and secrets.get("auth"):
            secrets["password"] = secrets.pop("auth")
        elif not secrets.get("password") and secrets.get("token"):
            secrets["password"] = secrets.pop("token")
        missing = not secrets.get("password")
    elif protocol == "tuic":
        missing = not (secrets.get("uuid") and secrets.get("password"))
    return secrets, missing


def _stringify_params(entry):
    params = {}
    for key, value in entry.items():
        if key in _KNOWN_KEYS:
            continue
        if isinstance(value, (dict, list)):
            params[key] = value
        elif value is None:
            continue
        else:
            params[key] = value
    return params


def _opts_params(entry, params):
    ws_opts = entry.get("ws-opts")
    if isinstance(ws_opts, dict):
        path = ws_opts.get("path")
        if path:
            params["path"] = str(path)
        headers = ws_opts.get("headers")
        if isinstance(headers, dict) and headers.get("Host"):
            params["host"] = str(headers.get("Host"))
    grpc_opts = entry.get("grpc-opts")
    if isinstance(grpc_opts, dict):
        service = grpc_opts.get("serviceName") or grpc_opts.get(
            "grpc-service-name"
        )
        if service:
            params["serviceName"] = str(service)
    http_opts = entry.get("http-opts")
    if isinstance(http_opts, dict):
        if http_opts.get("path"):
            params["path"] = str(http_opts.get("path"))
        host = http_opts.get("host")
        if isinstance(host, list) and host:
            params["host"] = ",".join(str(item) for item in host)
        elif host:
            params["host"] = str(host)
    h2_opts = entry.get("h2-opts")
    if isinstance(h2_opts, dict) and h2_opts.get("path"):
        params["path"] = str(h2_opts.get("path"))
    alpn = entry.get("alpn")
    if isinstance(alpn, list) and alpn:
        params["alpn"] = ",".join(str(item) for item in alpn)
    elif alpn:
        params["alpn"] = str(alpn)
    plugin = entry.get("plugin")
    if plugin:
        opts = entry.get("plugin-opts")
        if isinstance(opts, dict):
            params["plugin"] = {"name": str(plugin), "opts": opts}
        else:
            params["plugin"] = {"name": str(plugin), "opts": {}}
    if entry.get("flow"):
        params["flow"] = str(entry.get("flow"))
    fingerprint = entry.get("client-fingerprint") or entry.get("fingerprint")
    if fingerprint:
        params["client-fingerprint"] = str(fingerprint)
    reality_opts = entry.get("reality-opts")
    if isinstance(reality_opts, dict) and reality_opts:
        public_key = reality_opts.get("public-key") or reality_opts.get("publicKey")
        short_id = reality_opts.get("short-id") or reality_opts.get("shortId")
        if public_key is not None and str(public_key).strip():
            params["reality_public_key"] = str(public_key)
        if short_id is not None and str(short_id).strip():
            params["reality_short_id"] = str(short_id)
    alter_id = entry.get("alterId")
    if alter_id is None:
        alter_id = entry.get("alterid")
    if alter_id is not None:
        try:
            params["alter_id"] = int(str(alter_id).strip())
        except ValueError:
            params["alter_id"] = alter_id
    cipher = entry.get("cipher")
    if cipher:
        params["cipher"] = str(cipher)
    sni = entry.get("sni") or entry.get("servername") or entry.get("peer")
    if sni:
        params["sni"] = str(sni)
    obfs = entry.get("obfs")
    if obfs is not None and str(obfs).strip():
        params["obfs"] = str(obfs)
    obfs_param = entry.get("obfs-param") or entry.get("obfs_param")
    if obfs_param is not None and str(obfs_param).strip():
        params["obfs_param"] = str(obfs_param)
    congestion = (
        entry.get("congestion-controller")
        or entry.get("congestion_control")
        or entry.get("congestion")
    )
    if congestion is not None and str(congestion).strip():
        params["congestion_control"] = str(congestion)
    udp_relay = entry.get("udp-relay-mode") or entry.get("udp_relay_mode")
    if udp_relay is not None and str(udp_relay).strip():
        params["udp_relay_mode"] = str(udp_relay)
    skip_verify = entry.get("skip-cert-verify")
    if skip_verify is None:
        skip_verify = entry.get("allow_insecure")
    if skip_verify is not None:
        if isinstance(skip_verify, str):
            text = skip_verify.strip().lower()
            params["insecure"] = text in ("1", "true", "yes", "on")
        else:
            params["insecure"] = bool(skip_verify)
    return params


def _build_proxy(entry, index, source_id):
    ref = "proxies:{0}".format(index)
    protocol_raw = entry.get("type")
    if protocol_raw is None:
        return None, make_issue(
            source_id, UNSUPPORTED_PROTOCOL, "missing proxy type", ref
        )
    protocol = str(protocol_raw).strip().lower()
    if protocol == "hy2":
        protocol = "hysteria2"
    if not protocol:
        return None, make_issue(
            source_id, UNSUPPORTED_PROTOCOL, "missing proxy type", ref
        )
    if protocol not in (
        "vless",
        "vmess",
        "trojan",
        "ss",
        "hysteria2",
        "tuic",
        "socks5",
        "http",
        "snell",
        "wireguard",
        "ssh",
        "mieru",
        "anytls",
        "hysteria",
        "juicity",
    ):
        safe = sanitize_token(protocol_raw, limit=32)
        return None, make_issue(
            source_id,
            UNSUPPORTED_PROTOCOL,
            "unsupported protocol: {0}".format(safe),
            ref,
        )
    server = normalize_server(entry.get("server"))
    if not server:
        return None, make_issue(source_id, MISSING_SERVER, "missing server", ref)
    port = parse_port(entry.get("port"))
    if port is None:
        return None, make_issue(source_id, INVALID_PORT, "invalid port", ref)
    secrets, missing = _collect_creds(entry, protocol)
    if missing:
        return None, make_issue(
            source_id, MISSING_CREDENTIALS, "missing credentials", ref
        )
    params = _stringify_params(entry)
    params = _opts_params(entry, params)
    if protocol == "ss" and isinstance(params.get("cipher"), str):
        secrets["cipher"] = params.pop("cipher")
    remarks = sanitize_token(entry.get("name"), limit=256)
    transport = coerce_transport(entry.get("network"), protocol)
    security = _security_value(entry, protocol)
    fold_defaults(params)
    node = ParsedProxy(
        source_id=source_id,
        protocol=protocol,
        server=server,
        port=port,
        transport=transport,
        security=security,
        params=params,
        secrets=secrets,
        remarks=remarks,
    )
    return node, None


def parse_clash_yaml(text, source_id=""):
    raw = "" if text is None else str(text)
    if len(raw) > MAX_YAML_BYTES or len(raw.encode("utf-8")) > MAX_YAML_BYTES:
        return [], [
            make_issue(source_id, FILE_TOO_LARGE, "yaml payload exceeds 2MiB")
        ]
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        name = type(exc).__name__
        return [], [make_issue(source_id, INVALID_YAML, name)]
    if not isinstance(document, dict):
        return [], [
            make_issue(source_id, MISSING_PROXIES, "document is not a mapping")
        ]
    if _depth(document) > MAX_DEPTH:
        return [], [make_issue(source_id, INVALID_YAML, "nesting too deep")]
    entries = document.get("proxies")
    if not isinstance(entries, list) or not entries:
        return [], [
            make_issue(source_id, MISSING_PROXIES, "missing proxies list", "proxies")
        ]
    truncated = False
    if len(entries) > MAX_PROXIES:
        entries = entries[:MAX_PROXIES]
        truncated = True
    proxies = []
    issues = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            issues.append(
                make_issue(
                    source_id,
                    INVALID_YAML,
                    "proxy entry is not a mapping",
                    "proxies:{0}".format(index),
                )
            )
            continue
        node, issue = _build_proxy(entry, index, source_id)
        if node is not None:
            proxies.append(node)
        if issue is not None:
            issues.append(issue)
    if truncated:
        issues.append(
            make_issue(
                source_id,
                PROXY_LIMIT_EXCEEDED,
                "proxy list truncated to 2000 entries",
                "proxies",
            )
        )
    return proxies, issues
