"""Parser-local helpers.

Shared field coercion and issue codes live in :mod:`nodebench.core.fields`;
this module re-exports them for parsers and keeps parse-only helpers here.
"""

from __future__ import annotations

import base64
import re

from nodebench.core.fields import (
    ERROR_CODES,
    FILE_TOO_LARGE,
    INVALID_BASE64,
    INVALID_PORT,
    INVALID_ROW,
    INVALID_URI,
    INVALID_YAML,
    MISSING_CREDENTIALS,
    MISSING_PROXIES,
    MISSING_SERVER,
    PARSE_ERROR,
    PROXY_LIMIT_EXCEEDED,
    UNSUPPORTED_CONTENT,
    UNSUPPORTED_PROTOCOL,
    coerce_security,
    coerce_transport,
    default_security,
    default_transport,
    make_issue,
    normalize_server,
    parse_port,
    redact_uri_ref,
    sanitize_token,
)


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
        elif key == "insecure":
            # Default is certificate verification; explicit false is folded away
            # so the same node from different sources shares one fingerprint.
            if value is False or value in (0, "0", "false", ""):
                drop.append(key)
    for key in drop:
        params.pop(key, None)
    return params


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
    "b64decode_flexible",
    "coerce_security",
    "coerce_transport",
    "default_security",
    "default_transport",
    "fold_defaults",
    "make_issue",
    "normalize_server",
    "parse_port",
    "redact_uri_ref",
    "sanitize_token",
]
