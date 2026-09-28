from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nodebench.core.config import AppConfig, CfSourceConfig
from nodebench.core.schema import USER_SUPPLIED_LICENSE_TAG, ErrorInfo, RawItem, SourceReport
from nodebench.sources.base import BOM, make_error, resolve_base_dir
from nodebench.sources.http_utils import is_private_host

# Conservative DNS hostname: labels 1-63 chars, alnum + hyphen, not starting/ending with hyphen.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*$"
)

SOURCE_ID = "cf"
DEFAULT_PORT = 443
MAX_CF_FILE_BYTES = 2 * 1024 * 1024
TRUNCATION_CODE = "ip_limit_exceeded"

IP_COLUMNS = frozenset({"ip", "ip地址", "ipaddress", "address", "host", "ipv4", "ipv6"})
PORT_COLUMNS = frozenset({"port", "端口"})

PROBLEM_MESSAGES = {
    "empty_file": "candidate file has no content: {display}",
    "no_candidates": "no usable candidates in candidate file: {display}",
}


def _display_path(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.name


def _default_port(source: CfSourceConfig) -> int:
    value = getattr(source, "default_port", None)
    if isinstance(value, int) and 1 <= value <= 65535:
        return value
    return DEFAULT_PORT


def _normalize_header(value: str) -> str:
    return "".join(value.split()).lstrip(BOM).lower()


def _find_column(fields: list[str], names: frozenset[str]) -> int | None:
    for index, field in enumerate(fields):
        if _normalize_header(field) in names:
            return index
    return None


def _looks_like_header(fields: list[str]) -> bool:
    if len(fields) < 2:
        return False
    return any(
        _normalize_header(field) in IP_COLUMNS or _normalize_header(field) in PORT_COLUMNS
        for field in fields
    )


def _parse_port(value: str) -> int | None:
    try:
        port = int(value.strip())
    except ValueError:
        return None
    if 1 <= port <= 65535:
        return port
    return None


def _split_endpoint(token: str) -> tuple[str, int | None]:
    if token.count(":") == 1 and "/" not in token:
        host, _, tail = token.partition(":")
        port = _parse_port(tail)
        if host and port is not None:
            return host, port
    return token, None


def _validate_token(token: str, port: int | None) -> tuple[str, int | None] | None:
    token = token.strip()
    if not token:
        return None
    if "/" in token:
        try:
            ipaddress.ip_network(token, strict=False)
        except ValueError:
            return None
        return token, port
    try:
        ipaddress.ip_address(token)
        if is_private_host(token):
            return None
        return token, port
    except ValueError:
        pass
    # DNS hostname candidates (e.g. cloudflare.182682.xyz)
    if not _HOSTNAME_RE.match(token):
        return None
    lowered = token.lower()
    if is_private_host(lowered) or lowered.endswith((".local", ".localhost", ".internal", ".home.arpa")):
        return None
    return lowered, port


def _parse_line(
    line: str, ip_column: int, port_column: int | None
) -> tuple[str, int | None] | None:
    fields = [field.strip() for field in line.split(",")]
    token = fields[ip_column] if ip_column < len(fields) else ""
    port = None
    if port_column is not None and port_column < len(fields):
        port = _parse_port(fields[port_column])
    token, inline_port = _split_endpoint(token)
    if port is None:
        port = inline_port
    return _validate_token(token, port)


def _parse_candidates(
    text: str,
) -> tuple[list[tuple[str, int | None]], bool, str]:
    stripped = text.removeprefix(BOM)
    if not stripped.strip():
        return [], False, "empty_file"
    lines: list[str] = []
    for line in stripped.splitlines():
        cleaned = line.split("#", 1)[0].strip()
        if cleaned:
            lines.append(cleaned)
    if not lines:
        return [], False, "no_candidates"
    first_fields = [field.strip() for field in lines[0].split(",")]
    has_header = _looks_like_header(first_fields)
    ip_column = 0
    port_column: int | None = 1
    start = 0
    if has_header:
        found = _find_column(first_fields, IP_COLUMNS)
        if found is None:
            return [], has_header, "no_candidates"
        ip_column = found
        port_column = _find_column(first_fields, PORT_COLUMNS)
        start = 1
    entries: list[tuple[str, int | None]] = []
    for line in lines[start:]:
        parsed = _parse_line(line, ip_column, port_column)
        if parsed is not None:
            entries.append(parsed)
    if not entries:
        return [], has_header, "no_candidates"
    return entries, has_header, ""


def _format_endpoint(address: object, port: int) -> str:
    text = str(address)
    if ":" in text:
        return f"[{text}]:{port}"
    return f"{text}:{port}"


def _expand_candidates(
    entries: list[tuple[str, int | None]],
    budget: int,
    default_port: int,
    seen: set[tuple[str, int]],
) -> tuple[list[str], int, bool]:
    lines: list[str] = []
    remaining = budget
    truncated = False
    for token, port in entries:
        if remaining <= 0:
            truncated = True
            break
        port_value = port if port is not None else default_port
        if "/" not in token:
            key = (token, port_value)
            if key not in seen:
                seen.add(key)
                lines.append(_format_endpoint(token, port_value))
                remaining -= 1
            continue
        network = ipaddress.ip_network(token, strict=False)
        size = network.num_addresses
        take = size if size <= remaining else remaining
        if take < size:
            truncated = True
        if take == size:
            offsets = range(size)
        else:
            offsets = (index * size // take for index in range(take))
        for offset in offsets:
            if remaining <= 0:
                truncated = True
                break
            key = (str(network.network_address + offset), port_value)
            if key in seen:
                continue
            seen.add(key)
            lines.append(_format_endpoint(network.network_address + offset, port_value))
            remaining -= 1
    return lines, remaining, truncated


def _read_text(path: Path, display: str, errors: list[ErrorInfo]) -> str | None:
    try:
        is_file = path.is_file()
        exists = path.exists()
    except PermissionError:
        errors.append(
            make_error("file_permission", f"candidate file permission denied: {display}", True)
        )
        return None
    except OSError:
        errors.append(
            make_error("file_unreadable", f"candidate file cannot be read: {display}", True)
        )
        return None
    if not is_file:
        if exists:
            errors.append(
                make_error("file_invalid", f"candidate path is not a file: {display}")
            )
        else:
            errors.append(
                make_error("file_missing", f"candidate file does not exist: {display}")
            )
        return None
    try:
        size = path.stat().st_size
    except PermissionError:
        errors.append(
            make_error("file_permission", f"candidate file permission denied: {display}", True)
        )
        return None
    except OSError:
        errors.append(
            make_error("file_unreadable", f"candidate file cannot be read: {display}", True)
        )
        return None
    if size > MAX_CF_FILE_BYTES:
        errors.append(
            make_error(
                "file_too_large",
                f"candidate file exceeds {MAX_CF_FILE_BYTES} bytes: {display}",
            )
        )
        return None
    try:
        data = path.read_bytes()
    except PermissionError:
        errors.append(
            make_error("file_permission", f"candidate file permission denied: {display}", True)
        )
        return None
    except OSError:
        errors.append(
            make_error("file_unreadable", f"candidate file cannot be read: {display}", True)
        )
        return None
    try:
        return data.decode("utf-8").removeprefix(BOM)
    except UnicodeDecodeError:
        errors.append(
            make_error("decode_failed", f"candidate file is not valid utf-8: {display}")
        )
        return None


def collect_cf(
    cfg: AppConfig,
    ctx: Any = None,
    *,
    base_dir: str | Path | None = None,
) -> tuple[list[RawItem], list[SourceReport]]:
    """Import operator-listed CF candidates (IP, CIDR, CSV) within the configured budget."""
    source = cfg.sources.cf
    if not source.enabled:
        return [], []
    if not source.candidate_files:
        return [], [
            SourceReport(
                source_id=SOURCE_ID,
                ok=False,
                errors=[
                    make_error(
                        "no_files",
                        "cf source is enabled but candidate_files is empty",
                    )
                ],
            )
        ]
    base = resolve_base_dir(ctx, base_dir)
    limit = max(int(source.max_ips_per_run), 0)
    default_port = _default_port(source)
    license_tag = USER_SUPPLIED_LICENSE_TAG
    resolved: list[tuple[Path, str]] = []
    for raw in source.candidate_files:
        path = Path(raw)
        if not path.is_absolute():
            path = base / path
        resolved.append((path, _display_path(path, base)))
    scope = ", ".join(display for _, display in resolved)
    errors: list[ErrorInfo] = []
    items: list[RawItem] = []
    seen: set[tuple[str, int]] = set()
    budget = limit
    fetched_at = datetime.now(timezone.utc)
    for path, display in resolved:
        text = _read_text(path, display, errors)
        if text is None:
            continue
        entries, has_header, problem = _parse_candidates(text)
        if problem:
            template = PROBLEM_MESSAGES[problem]
            errors.append(make_error(problem, template.format(display=display)))
            continue
        added, budget, truncated = _expand_candidates(entries, budget, default_port, seen)
        if added:
            # Expanded payload is always HOST:PORT lines, never a CSV body.
            items.append(
                RawItem(
                    source_id=SOURCE_ID,
                    content_type="endpoint_list",
                    payload="\n".join(added),
                    fetched_at=fetched_at,
                    license_tag=license_tag,
                    source_ref=display,
                )
            )
        if truncated:
            errors.append(
                make_error(
                    TRUNCATION_CODE,
                    f"candidate limit {limit} reached; remaining candidates truncated at {display}",
                )
            )
            break
    ok = bool(items) and all(error.code == TRUNCATION_CODE for error in errors)
    report = SourceReport(
        source_id=SOURCE_ID,
        ok=ok,
        fetched=len(items),
        errors=errors,
        scope=scope,
    )
    return items, [report]


__all__ = [
    "SOURCE_ID",
    "USER_SUPPLIED_LICENSE_TAG",
    "DEFAULT_PORT",
    "MAX_CF_FILE_BYTES",
    "TRUNCATION_CODE",
    "collect_cf",
]
