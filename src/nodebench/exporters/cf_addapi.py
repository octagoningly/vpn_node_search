from __future__ import annotations

from typing import Iterable, Sequence

CF_ADDAPI_NAME = "cf-addapi.txt"


def _host(address: str) -> str:
    text = str(address or "").strip()
    return "[{0}]".format(text) if ":" in text else text


def _remark(value) -> str:
    return str(value or "").replace("#", "").strip()


def build_addapi(rows: Iterable[Sequence[object]]) -> str:
    lines = []
    for row in rows:
        address, port, remark = row[0], int(row[1]), _remark(row[2] if len(row) > 2 else "")
        line = "{0}:{1}".format(_host(address), int(port))
        if remark:
            line = "{0}#{1}".format(line, remark)
        lines.append(line)
    if not lines:
        return ""
    return "".join("{0}\n".format(line) for line in lines)
