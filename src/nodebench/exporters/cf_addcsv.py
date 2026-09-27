from __future__ import annotations

import csv
import io
from typing import Iterable, Sequence

from nodebench.parsers.csv import HEADER_FULL

CF_ADDCSV_NAME = "cf-addcsv.csv"


def _flag(value: object) -> str:
    return "true" if bool(value) else "false"


def _latency(value: object) -> str:
    if value is None or value == "":
        return ""
    return "{0:.1f}".format(float(value))


def _speed(value: object) -> str:
    if value is None or value == "":
        return ""
    return "{0:.2f}".format(float(value))


def build_addcsv(rows: Iterable[Sequence[object]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(HEADER_FULL)
    for row in rows:
        address, port, origin, tls, datacenter, region, city, latency, speed = row
        writer.writerow(
            [
                str(address),
                str(int(port)),
                str(origin or ""),
                _flag(tls),
                str(datacenter or ""),
                str(region or ""),
                str(city or ""),
                _latency(latency),
                _speed(speed),
            ]
        )
    return buffer.getvalue()
