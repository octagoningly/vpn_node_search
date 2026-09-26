from __future__ import annotations

import csv
import io
import ipaddress

from nodebench.core.schema import ParsedEndpoint
from nodebench.parsers.common import (
    INVALID_ROW,
    INVALID_PORT,
    make_issue,
    parse_port,
)

HEADER_FULL = [
    "IP地址",
    "端口",
    "回源端口",
    "TLS",
    "数据中心",
    "地区",
    "城市",
    "TCP延迟(ms)",
    "速度(MB/s)",
]
HEADER_TWO = ["ip", "port"]

_TRUE = frozenset({"true", "1", "yes", "t", "y"})
_FALSE = frozenset({"false", "0", "no", "f", "n"})


def _clean_cells(row):
    cells = [str(cell).strip() for cell in row]
    if cells:
        cells[0] = cells[0].lstrip("﻿")
    return cells


def _split_bracket(value):
    text = str(value).strip()
    if not text.startswith("["):
        return text, None
    close = text.find("]")
    if close == -1:
        return "", None
    host = text[1:close]
    tail = text[close + 1 :]
    port_value = None
    if tail.startswith(":"):
        port_value = tail[1:]
    return host, port_value


def _valid_ip(value):
    try:
        ipaddress.ip_address(str(value).strip())
        return True
    except ValueError:
        return False


def _parse_tls(value):
    text = str(value).strip().lower()
    if text in _TRUE:
        return True, None
    if text in _FALSE:
        return False, None
    return None, True


def _params_from_nine(cells):
    params = {}
    origin_port = parse_port(cells[1])
    if origin_port is None:
        params["origin_port"] = cells[1]
    else:
        params["origin_port"] = origin_port
    for key, value in (
        ("datacenter", cells[4]),
        ("region", cells[5]),
        ("city", cells[6]),
    ):
        if value:
            params[key] = value
    return params


def _build_endpoint(cells, mode, line_no, source_id):
    ref = "line:{0}".format(line_no)
    if mode == 9:
        host, embedded_port = _split_bracket(cells[0])
        params = _params_from_nine(cells)
        tls_value, tls_error = _parse_tls(cells[3])
        if tls_error:
            return None, make_issue(
                source_id, INVALID_ROW, "invalid tls flag", ref
            )
    else:
        host, embedded_port = _split_bracket(cells[0])
        params = {}
        if cells[1]:
            params["origin_port"] = parse_port(cells[1]) or cells[1]
        tls_value = False
    if not host or not _valid_ip(host):
        return None, make_issue(source_id, INVALID_ROW, "invalid ip address", ref)
    port = None
    port_text = cells[1]
    if port_text:
        port = parse_port(port_text)
        if port is None:
            return None, make_issue(source_id, INVALID_PORT, "invalid port", ref)
    else:
        port = parse_port(embedded_port) if embedded_port else None
        if port is None:
            return None, make_issue(source_id, INVALID_PORT, "invalid port", ref)
    node = ParsedEndpoint(
        source_id=source_id,
        address=str(host).strip().lower(),
        port=port,
        target_host="",
        tls=bool(tls_value),
        params=params,
        remarks="",
    )
    return node, None


def parse_endpoint_csv(text, source_id=""):
    raw = "" if text is None else str(text)
    reader = csv.reader(io.StringIO(raw, newline=""))
    rows = []
    for index, row in enumerate(reader, start=1):
        cells = _clean_cells(row)
        if not any(cells):
            continue
        rows.append((index, cells))
    if not rows:
        return [], []
    _, first = rows[0]
    if first == HEADER_FULL:
        mode = 9
        rows = rows[1:]
    elif [cell.lower() for cell in first] == HEADER_TWO:
        mode = 2
        rows = rows[1:]
    else:
        mode = 2
    expected = 9 if mode == 9 else 2
    endpoints = []
    issues = []
    for line_no, cells in rows:
        if len(cells) != expected:
            issues.append(
                make_issue(
                    source_id,
                    INVALID_ROW,
                    "unexpected column count",
                    "line:{0}".format(line_no),
                )
            )
            continue
        node, issue = _build_endpoint(cells, mode, line_no, source_id)
        if node is not None:
            endpoints.append(node)
        if issue is not None:
            issues.append(issue)
    return endpoints, issues
