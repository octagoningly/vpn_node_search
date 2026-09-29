"""Cross-layer field coercion and normalization helpers.

These are shared by parsers, normalize and exporters. Keep them free of
package-local imports so lower layers can depend on ``core`` only.
"""

from __future__ import annotations

import re

from nodebench.core.schema import ParseIssue

# Shared validation/parse issue codes (ParseIssue.code vocabulary).
INVALID_URI = "invalid_uri"
INVALID_PORT = "invalid_port"
MISSING_CREDENTIALS = "missing_credentials"
UNSUPPORTED_PROTOCOL = "unsupported_protocol"
INVALID_BASE64 = "invalid_base64"
INVALID_YAML = "invalid_yaml"
INVALID_ROW = "invalid_row"
MISSING_SERVER = "missing_server"
MISSING_PROXIES = "missing_proxies"
FILE_TOO_LARGE = "file_too_large"
PROXY_LIMIT_EXCEEDED = "proxy_limit_exceeded"
UNSUPPORTED_CONTENT = "unsupported_content"
PARSE_ERROR = "parse_error"

ERROR_CODES = frozenset(
    {
        INVALID_URI,
        INVALID_PORT,
        MISSING_CREDENTIALS,
        UNSUPPORTED_PROTOCOL,
        INVALID_BASE64,
        INVALID_YAML,
        INVALID_ROW,
        MISSING_SERVER,
        MISSING_PROXIES,
        FILE_TOO_LARGE,
        PROXY_LIMIT_EXCEEDED,
        UNSUPPORTED_CONTENT,
        PARSE_ERROR,
    }
)

_PORT_RE = re.compile(r"[0-9]+")
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://")


def make_issue(source_id, code, message, raw_ref=""):
    return ParseIssue(
        source_id=source_id,
        code=code,
        message_redacted=message,
        raw_ref=raw_ref,
    )


def parse_port(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if 1 <= value <= 65535:
            return value
        return None
    if isinstance(value, float):
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _PORT_RE.fullmatch(text) is None:
        return None
    number = int(text, 10)
    if 1 <= number <= 65535:
        return number
    return None


def normalize_server(host):
    if host is None:
        return ""
    text = str(host).replace("[", "").replace("]", "").strip().lower()
    return text


def sanitize_token(value, limit=64):
    if value is None:
        return ""
    text = str(value)
    cleaned = "".join(ch for ch in text if ord(ch) >= 32 and ord(ch) != 127)
    return cleaned[:limit]


def default_transport(protocol):
    text = "" if protocol is None else str(protocol).strip().lower()
    if text in ("hysteria2", "hy2", "tuic"):
        return "udp"
    return "tcp"


def default_security(protocol):
    text = "" if protocol is None else str(protocol).strip().lower()
    if text in ("trojan", "hysteria2", "hy2", "tuic"):
        return "tls"
    return "none"


def _coerce(value, protocol, fallback):
    if value is None:
        return fallback
    text = str(value).strip().lower()
    if text == "":
        return fallback
    if text == "true":
        return "tls"
    if text in ("false", "none"):
        return "none"
    if text == "reality":
        return "reality"
    return text


def coerce_transport(value, protocol):
    return _coerce(value, protocol, default_transport(protocol))


def coerce_security(value, protocol):
    return _coerce(value, protocol, default_security(protocol))


def redact_uri_ref(line):
    text = "" if line is None else str(line).strip()
    match = _SCHEME_RE.match(text)
    if match is None:
        return "<unknown>://…"
    return "<{0}>://…".format(match.group(1).lower())


__all__ = [
    "ERROR_CODES",
    "FILE_TOO_LARGE",
    "INVALID_BASE64",
    "INVALID_PORT",
    "INVALID_ROW",
    "INVALID_URI",
    "INVALID_YAML",
    "MISSING_CREDENTIALS",
    "MISSING_PROXIES",
    "MISSING_SERVER",
    "PARSE_ERROR",
    "PROXY_LIMIT_EXCEEDED",
    "UNSUPPORTED_CONTENT",
    "UNSUPPORTED_PROTOCOL",
    "coerce_security",
    "coerce_transport",
    "default_security",
    "default_transport",
    "make_issue",
    "normalize_server",
    "parse_port",
    "redact_uri_ref",
    "sanitize_token",
]
