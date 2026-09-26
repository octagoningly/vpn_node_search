import base64
import re

from nodebench.core.schema import ParseIssue

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

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://")
_SCHEME_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_PORT_RE = re.compile(r"[0-9]+")


def make_issue(source_id, code, message, raw_ref=""):
    return ParseIssue(
        source_id=source_id,
        code=code,
        message_redacted=message,
        raw_ref=raw_ref,
    )


def redact_uri_ref(line):
    text = "" if line is None else str(line).strip()
    match = _SCHEME_RE.match(text)
    if match is None:
        return "<unknown>://…"
    return "<{0}>://…".format(match.group(1).lower())


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


def b64decode_flexible(text):
    if not isinstance(text, str):
        return None
    compact = re.sub(r"\s+", "", text)
    compact = compact.replace("-", "+").replace("_", "/")
    if len(compact) % 4 == 1:
        return None
    padding = "=" * ((4 - len(compact) % 4) % 4)
    try:
        return base64.b64decode((compact + padding).encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return None


def fold_defaults(params):
    drop = []
    for key, value in params.items():
        if key in ("encryption", "headerType", "header-type"):
            if isinstance(value, str):
                text = value.strip().lower()
            elif value is None:
                text = ""
            else:
                text = str(value).lower()
            if text in ("", "none"):
                drop.append(key)
        elif key == "alter_id":
            if value in (0, "0", ""):
                drop.append(key)
    for key in drop:
        params.pop(key, None)
    return params


def sanitize_token(value, limit=64):
    if value is None:
        return ""
    text = str(value)
    cleaned = "".join(
        ch for ch in text if ord(ch) >= 32 and ord(ch) != 127
    )
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
