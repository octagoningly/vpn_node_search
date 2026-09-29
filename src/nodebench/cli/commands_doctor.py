from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from nodebench.cli.common import (
    BINARY_NAMES,
    CREDENTIAL_ENV_NAMES,
    DOCTOR_MESSAGE,
    DOCTOR_MESSAGE_PENDING,
    WRITE_PROBE_NAME,
    _error_text,
    _load,
    _project_root,
)
from nodebench.core.config import AppConfig
from nodebench.probes import (
    LEVEL_WARN,
    as_check,
    check_cf_prereqs,
    check_proxy_prereqs,
)


def _check_output_dir(root: Path, config: AppConfig | None) -> tuple[str, str, str, str]:
    raw = config.output_dir if config is not None else "output"
    target = Path(raw) if raw else Path("output")
    if not target.is_absolute():
        target = root / target
    probe = target / WRITE_PROBE_NAME
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
    except OSError as err:
        return ("fail", "output", _error_text(err), f"grant write access to {target}")
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    return ("ok", "output", str(target), "")


def _doctor_checks(args: argparse.Namespace) -> list[tuple[str, str, str, str]]:
    checks: list[tuple[str, str, str, str]] = []
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info >= (3, 12):
        checks.append(("ok", "python", version, ""))
    else:
        checks.append(
            (
                "fail",
                "python",
                f"{version} is older than 3.12",
                "install python 3.12 or newer",
            )
        )
    root = _project_root()
    shipped: AppConfig | None = None
    try:
        shipped = _load(args, env={})
    except Exception as err:
        checks.append(
            (
                "fail",
                "config",
                _error_text(err),
                "repair config/default.yaml and config/sources.yaml",
            )
        )
    if shipped is not None:
        checks.append(("ok", "config", f"profile {shipped.profile} loads", ""))
    config: AppConfig | None = None
    try:
        config = _load(args)
    except Exception as err:
        checks.append(
            (
                "fail",
                "environment",
                _error_text(err),
                "unset or fix the NODEBENCH_* environment variables",
            )
        )
    if config is not None:
        checks.append(("ok", "environment", "effective config loads", ""))
    input_dir = root / "input"
    try:
        sample_files = [
            path
            for path in input_dir.rglob("*")
            if path.is_file() and not any(part.startswith(".") for part in path.parts)
        ]
    except OSError as err:
        checks.append(("fail", "input", _error_text(err), "check the input directory"))
    else:
        if sample_files:
            checks.append(("ok", "input", f"{len(sample_files)} sample files", ""))
        else:
            checks.append(
                ("fail", "input", "no readable sample files", "add samples under input/")
            )
    checks.append(_check_output_dir(root, config))
    if config is not None:
        checks.extend(
            as_check(report) for report in check_proxy_prereqs(config, root)
        )
        checks.extend(as_check(report) for report in check_cf_prereqs(config, root))
    for name in BINARY_NAMES:
        found = shutil.which(name)
        if found:
            checks.append(("ok", name, found, ""))
        else:
            checks.append(
                ("warn", name, "not found on PATH", f"install {name} and add it to PATH")
            )
    for name in CREDENTIAL_ENV_NAMES:
        state = "set" if os.environ.get(name) else "not set"
        checks.append(("info", name.lower(), state, ""))
    return checks


def _cmd_doctor(args: argparse.Namespace) -> int:
    try:
        checks = _doctor_checks(args)
    except Exception as err:
        print(f"[FAIL] doctor: {_error_text(err)} (fix: repair the installation)")
        return 2
    failures = 0
    for level, name, detail, fix in checks:
        line = f"[{level.upper()}] {name}: {detail}"
        if fix:
            line += f" (fix: {fix})"
        print(line)
        if level == "fail":
            failures += 1
    if failures:
        print(f"{failures} failing check(s)")
        return 2
    pending = any(level == LEVEL_WARN for level, _name, _detail, _fix in checks)
    print(DOCTOR_MESSAGE_PENDING if pending else DOCTOR_MESSAGE)
    return 0
