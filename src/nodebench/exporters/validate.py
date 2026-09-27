from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from nodebench.core.context import MIN_SECRET_LENGTH, registered_secrets

CREDENTIAL_URI_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s@]+@")


def scan_text(name: str, text: str) -> list[str]:
    errors: list[str] = []
    if CREDENTIAL_URI_PATTERN.search(text):
        errors.append("credential uri in {0}".format(name))
    for secret in registered_secrets():
        if len(secret) < MIN_SECRET_LENGTH:
            continue
        if secret.isdigit():
            continue
        if secret in text:
            errors.append("registered secret match in {0}".format(name))
            break
    return errors


def scan_files(directory: str | Path, names: Iterable[str]) -> list[str]:
    base = Path(directory)
    errors: list[str] = []
    seen: set[str] = set()
    for name in names:
        path = base / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for message in scan_text(name, text):
            if message not in seen:
                seen.add(message)
                errors.append(message)
    return errors
