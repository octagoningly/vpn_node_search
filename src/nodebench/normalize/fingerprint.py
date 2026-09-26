from __future__ import annotations

import hashlib
import hmac
import json
import os

from nodebench.core.schema import FINGERPRINT_VERSION

DEFAULT_FINGERPRINT_KEY = "nodebench-fingerprint-v1"

_PROXY_DOMAIN = "proxy|v1"
_ENDPOINT_DOMAIN = "endpoint|v1"


def _fingerprint_key():
    configured = os.environ.get("NODEBENCH_FINGERPRINT_KEY")
    if configured is None or configured == "":
        configured = DEFAULT_FINGERPRINT_KEY
    return configured.encode("utf-8")


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


def _text(value):
    if value is None:
        return ""
    return str(value).strip().lower()


def _port_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
        return stripped
    return value


def _mapping(value):
    if isinstance(value, dict):
        return dict(value)
    return {}


def _digest(payload):
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    mac = hmac.new(_fingerprint_key(), encoded, hashlib.sha256)
    return "hmac-sha256:" + mac.hexdigest()


def fingerprint_proxy(proxy):
    payload = {
        "domain": _PROXY_DOMAIN,
        "fingerprint_version": FINGERPRINT_VERSION,
        "protocol": _text(_field(proxy, "protocol")),
        "server": _text(_field(proxy, "server")),
        "port": _port_value(_field(proxy, "port")),
        "transport": _text(_field(proxy, "transport")),
        "security": _text(_field(proxy, "security")),
        "params": _mapping(_field(proxy, "params", {})),
        "secrets": _mapping(_field(proxy, "secrets", {})),
    }
    return _digest(payload)


def fingerprint_endpoint(endpoint):
    payload = {
        "domain": _ENDPOINT_DOMAIN,
        "fingerprint_version": FINGERPRINT_VERSION,
        "address": _text(_field(endpoint, "address")),
        "port": _port_value(_field(endpoint, "port")),
        "target_host": _text(_field(endpoint, "target_host")),
        "tls": bool(_field(endpoint, "tls", False)),
    }
    return _digest(payload)


def make_item_id(fingerprint):
    return fingerprint
