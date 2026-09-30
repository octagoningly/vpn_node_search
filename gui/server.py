"""Local HTTP API + static server for NodeBench web UI.

Run:  uv run python gui/server.py
Open: http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = Path(__file__).resolve().parent / "web"
# Frozen exe: assets live next to the executable (or in _internal/gui/web).
if getattr(sys, "frozen", False):
    base = Path(sys.executable).resolve().parent
    for cand in (base / "gui" / "web", base / "_internal" / "gui" / "web"):
        if cand.is_dir():
            WEB = cand
            ROOT = base
            break
ENV_PATH = ROOT / ".env"
CONFIG_PATH = ROOT / "config" / "default.yaml"
POOL_PATH = ROOT / "candidates" / "user-import.txt"
THEME_PATH = ROOT / "ui-theme.txt"
PORT = 8765
_bound: dict = {"port": None, "error": ""}

# run state
_state = {
    "running": False,
    "log": "",
    "ok": False,
    "code": None,
    "stage_index": -1,
    "summary": "",
    "ranked": 0,
    "published": False,
    "publish_error": "",
    "run_id": "",
}


# ── env / config helpers ────────────────────────────────────────────────
def load_env() -> dict[str, str]:
    data: dict[str, str] = {}
    if ENV_PATH.is_file():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def save_env(values: dict[str, str]) -> None:
    merged = load_env()
    for k, v in values.items():
        # allow explicit empty clear only for secrets via key names ending in _CLEAR
        if v or k.startswith("SET_"):
            merged[k] = v
    order = [
        "GITHUB_TOKEN",
        "ABUSEIPDB_KEY",
        "IPINFO_TOKEN",
        "GITHUB_LOGIN",
        "GITHUB_REPO",
    ]
    lines = ["# NodeBench local secrets — never commit"]
    for key in order:
        lines.append(f"{key}={merged.get(key, '')}")
    # keep any other keys we didn't anticipate
    for key, val in merged.items():
        if key not in order and not key.startswith("#"):
            lines.append(f"{key}={val}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_theme() -> str:
    """Theme must outlive WebView2 localStorage — keep it on disk."""
    try:
        if THEME_PATH.is_file():
            value = THEME_PATH.read_text(encoding="utf-8").strip().lower()
            if value in {"dark", "light"}:
                return value
    except OSError:
        pass
    return "light"


def save_theme(value: str) -> str:
    theme = "dark" if str(value).strip().lower() == "dark" else "light"
    THEME_PATH.write_text(theme + "\n", encoding="utf-8")
    return theme


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    import yaml

    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}


def save_config(data: dict) -> None:
    import yaml

    CONFIG_PATH.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ── GitHub ──────────────────────────────────────────────────────────────
def gh_api(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    token = load_env().get("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("未配置 GITHUB_TOKEN，请先认证")
    from netproxy import request_json

    return request_json(
        f"https://api.github.com{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        payload=payload,
    )


def repo_raw_urls(name: str, login: str | None = None) -> dict:
    if not login:
        user = gh_api("/user")
        login = user["login"]
    base = f"https://raw.githubusercontent.com/{login}/{name}/public"
    return {
        "addapi": f"{base}/cf-addapi.txt",
        "addcsv": f"{base}/cf-addcsv.csv",
    }


def save_repo_state(login: str, repo: str) -> None:
    """Persist login/repo so raw URLs can be assembled without another API call."""
    values: dict[str, str] = {"GITHUB_LOGIN": login}
    if repo:
        values["GITHUB_REPO"] = repo
    save_env(values)


def current_publish_urls() -> dict:
    env = load_env()
    login = env.get("GITHUB_LOGIN", "").strip()
    repo = env.get("GITHUB_REPO", "").strip()
    if login and repo:
        return repo_raw_urls(repo, login=login)
    return {
        "addapi": "https://raw.githubusercontent.com/<user>/<repo>/public/cf-addapi.txt",
        "addcsv": "https://raw.githubusercontent.com/<user>/<repo>/public/cf-addcsv.csv",
    }


# ── autostart (Windows) ─────────────────────────────────────────────────
AUTOSTART_NAME = "NodeBench"


def _autostart_cmd() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    return f'"{sys.executable}" "{ROOT / "gui" / "desktop.py"}"'


def autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        )
        try:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except OSError:
        return False


def set_autostart(enable: bool) -> None:
    if os.name != "nt":
        return
    import winreg

    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE | winreg.KEY_READ,
    )
    try:
        if enable:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, _autostart_cmd())
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


# ── run worker ──────────────────────────────────────────────────────────
def _newest_run_dir() -> Path | None:
    base = _output_base()
    if not base.is_dir():
        return None
    runs = [
        p
        for p in base.iterdir()
        if p.is_dir() and re.match(r"^\d{8}T\d{6}Z-", p.name)
    ]
    if not runs:
        return None
    return sorted(runs, key=lambda p: p.name)[-1]


def _read_run_report(run_dir: Path) -> dict:
    for name in ("run-report.json", "export/report.json", "report.json"):
        path = run_dir / name
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    return {}


def _report_ranked(report: dict) -> int:
    stages = report.get("stages") or {}
    score = stages.get("score") or {}
    counts = score.get("counts") or {}
    return int(counts.get("ranked") or 0)


def _report_probe_usable(report: dict) -> int:
    probe = report.get("probe") or {}
    total = 0
    for node in probe.values():
        if isinstance(node, dict):
            total += int(node.get("usable_real") or 0)
    return total


def push_public_branch() -> tuple[bool, str]:
    """Force-push cf-addapi/cf-addcsv to the user's GitHub public branch."""
    env = load_env()
    token = (env.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    login = (env.get("GITHUB_LOGIN") or "").strip()
    repo = (env.get("GITHUB_REPO") or "").strip()
    if token and not login:
        try:
            user = gh_api("/user")
            login = str(user.get("login") or "").strip()
        except Exception:  # noqa: BLE001
            login = ""
    if token and login and not repo:
        # Prefer a previously known name, else first public repo.
        try:
            repos = gh_api("/user/repos?per_page=100&sort=updated")
            names = [str(r.get("name") or "") for r in repos if isinstance(r, dict)]
            repo = next((n for n in names if n == "cf-ip-pool"), names[0] if names else "")
        except Exception:  # noqa: BLE001
            repo = ""
    if login and repo:
        save_repo_state(login, repo)
    if not token or not login or not repo:
        return False, "缺少 GITHUB_TOKEN / GITHUB_LOGIN / GITHUB_REPO（请在密钥页重新认证）"
    src = _output_base() / "latest"
    files = ["cf-addapi.txt", "cf-addcsv.csv"]
    present = [f for f in files if (src / f).is_file() and (src / f).stat().st_size > 0]
    if not present:
        return False, "latest 目录没有可发布的 cf-addapi/cf-addcsv"
    work = _output_base() / "_public_branch"
    try:
        if work.exists():
            import shutil

            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        for name in present:
            (work / name).write_bytes((src / name).read_bytes())
        (work / "README.md").write_text(
            f"# {repo}\n\nAuto-published by NodeBench. Do not edit by hand.\n",
            encoding="utf-8",
        )
        git = ["git", "-C", str(work)]
        auth_url = f"https://x-access-token:{token}@github.com/{login}/{repo}.git"
        cmds = [
            git + ["init", "-b", "public"],
            git + ["config", "user.email", "nodebench@local"],
            git + ["config", "user.name", "nodebench-publish"],
            git + ["add", "-A"],
            git + ["commit", "-m", f"publish: nodebench {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
            git + ["remote", "add", "origin", auth_url],
            git + ["push", "-u", "origin", "public", "--force"],
        ]
        for cmd in cmds:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=(os.name == "nt" and cmd[0] == "git"),
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()[-400:]
                return False, f"git {cmd[1]} 失败: {err}"
        return True, f"https://raw.githubusercontent.com/{login}/{repo}/public/cf-addapi.txt"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def run_scheduler_cli(*args: str) -> dict:
    """Invoke `nodebench scheduler …` — GUI stays a process shell (no biz imports)."""
    proc = subprocess.run(
        ["uv", "run", "nodebench", "scheduler", *args, "--json"],
        cwd=_project_root(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=(os.name == "nt"),
    )
    out = (proc.stdout or "").strip()
    if not out:
        return {"ok": False, "error": (proc.stderr or "no output")[-400:], "code": proc.returncode}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "error": out[-400:], "code": proc.returncode}
    if isinstance(data, dict):
        data.setdefault("ok", proc.returncode == 0)
        return data
    return {"ok": proc.returncode == 0, "items": data}


def run_worker() -> None:
    _state.update(
        running=True,
        log="启动 nodebench run --profile auto-collect…\n",
        stage_index=0,
        summary="",
        ranked=0,
        published=False,
        publish_error="",
        run_id="",
        ok=False,
        code=None,
    )
    try:
        proc = subprocess.run(
            ["uv", "run", "nodebench", "run", "--profile", "auto-collect"],
            cwd=_project_root(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=(os.name == "nt"),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        _state["log"] += out[-12000:]
        _state["code"] = proc.returncode

        _state["stage_index"] = 1
        _state["log"] += "\n—— 解析运行结果 ——\n"
        run_dir = _newest_run_dir()
        report = _read_run_report(run_dir) if run_dir else {}
        ranked = _report_ranked(report)
        usable = _report_probe_usable(report)
        run_id = str(report.get("run_id") or (run_dir.name if run_dir else ""))
        _state["run_id"] = run_id
        _state["ranked"] = ranked
        _state["stage_index"] = 2

        if ranked > 0:
            _state["log"] += f"入榜 {ranked} 条，准备发布到 public 分支…\n"
            _state["stage_index"] = 3
            ok_pub, msg = push_public_branch()
            _state["published"] = ok_pub
            if ok_pub:
                _state["log"] += f"✓ 已发布：{msg}\n"
                _state["summary"] = f"本轮入榜 {ranked} 条，已发布到 public 分支"
                _state["stage_index"] = 4
                _state["ok"] = True
            else:
                _state["publish_error"] = msg
                _state["log"] += f"✗ 发布失败：{msg}\n"
                _state["summary"] = f"本轮入榜 {ranked} 条，但发布失败：{msg}"
                _state["ok"] = False
        else:
            reason = "测速无可用节点" if usable == 0 else f"实测可用 {usable} 条但均未过筛选"
            _state["log"] += f"本轮 0 入榜（{reason}），无可发布内容\n"
            _state["summary"] = f"本轮 0 入榜（{reason}），未发布"
            _state["stage_index"] = 3
            _state["ok"] = False
            if proc.returncode != 0:
                _state["log"] += f"nodebench 退出码 {proc.returncode}（探测失败/部分完成属正常）\n"
    except Exception as exc:  # noqa: BLE001
        _state["log"] += f"\n启动失败: {exc}\n"
        _state["code"] = -1
        _state["ok"] = False
        _state["summary"] = f"运行失败：{exc}"
    finally:
        _state["running"] = False


# ── results ─────────────────────────────────────────────────────────────
def _project_root() -> Path:
    """Root where nodebench writes ``output/`` (differs from GUI ROOT when frozen).

    The frozen exe lives under dist/NodeBench, but the CLI resolves output
    against the real project (the directory with config/default.yaml).
    """
    if (ROOT / "config" / "default.yaml").is_file():
        return ROOT
    for cand in (ROOT, *ROOT.parents):
        if (cand / "config" / "default.yaml").is_file():
            return cand
        if (cand / "pyproject.toml").is_file() and (cand / "src" / "nodebench").is_dir():
            return cand
    for cand in (Path.cwd(), *Path.cwd().parents):
        if (cand / "config" / "default.yaml").is_file():
            return cand
    return ROOT


def _output_base() -> Path:
    return _project_root() / "output"


def _read_addapi_lines(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    items: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        addr, _, remark = line.partition("#")
        host, _, port = addr.rpartition(":")
        parts = remark.split("-") if remark else []
        items.append(
            {
                "address": host,
                "port": port,
                "speed": parts[0] if len(parts) > 0 else "",
                "purity": parts[1] if len(parts) > 1 else "",
                "stability": parts[2] if len(parts) > 2 else "",
                "country": parts[3] if len(parts) > 3 else "",
                "score": "",
                "remark": remark,
            }
        )
    return items


def _load_score_index(scored_path: Path) -> dict[tuple[str, str], dict]:
    """Map (address, port) → ranked metrics from a run's scored.json."""
    if not scored_path.is_file():
        return {}
    try:
        data = json.loads(scored_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    index: dict[tuple[str, str], dict] = {}
    for key in ("endpoints", "proxies"):
        for node in data.get(key) or []:
            if not isinstance(node, dict) or node.get("status") != "ranked":
                continue
            addr = str(node.get("address") or "")
            port = str(node.get("port") or "")
            if not addr:
                continue
            score = node.get("score")
            try:
                score_txt = f"{float(score):.2f}" if score is not None else ""
            except (TypeError, ValueError):
                score_txt = ""
            index[(addr, port)] = {
                "score": score_txt,
                "speed": node.get("speed_mb_s"),
                "latency": node.get("latency_ms"),
                "loss": node.get("loss_pct"),
                "availability": node.get("availability_rate"),
                "rank": node.get("rank"),
                "region": str(node.get("region") or ""),
                "country_code": str(node.get("country_code") or ""),
            }
    return index


def _all_run_dirs() -> list[Path]:
    base = _output_base()
    if not base.is_dir():
        return []
    runs = [
        p
        for p in base.iterdir()
        if p.is_dir() and re.match(r"^\d{8}T\d{6}Z-", p.name)
    ]
    return sorted(runs, key=lambda p: p.name, reverse=True)


def load_results() -> dict:
    """Best available results: published latest, else newest non-empty run.

    Walk back through runs so a 0-入榜 round does not hide the previous
    good measurement — the user still needs to see what they can paste.
    """
    latest = _output_base() / "latest" / "cf-addapi.txt"
    latest_items = _read_addapi_lines(latest)
    if latest_items:
        scored = _load_score_index(_output_base() / "latest" / "scored.json")
        if not scored:
            run_dir = _newest_run_dir()
            if run_dir is not None:
                scored = _load_score_index(run_dir / "scored.json")
        items = _merge_scores(latest_items, scored)
        return {
            "items": items,
            "source": "output/latest",
            "published": True,
            "note": "",
            "count": len(items),
            "stale": False,
        }

    for run_dir in _all_run_dirs():
        exp = run_dir / "export" / "cf-addapi.txt"
        items = _read_addapi_lines(exp)
        if items:
            scored = _load_score_index(run_dir / "scored.json")
            items = _merge_scores(items, scored)
            newest = _newest_run_dir()
            is_latest_run = newest is not None and newest.name == run_dir.name
            if is_latest_run:
                note = "来自最近一次运行的导出（未发布到 latest）"
                stale = False
            else:
                note = f"本轮无入榜结果 — 显示 {run_dir.name} 的上次有效数据"
                stale = True
            return {
                "items": items,
                "source": run_dir.name,
                "published": False,
                "note": note,
                "count": len(items),
                "stale": stale,
            }
        scored_items = _scored_to_rows(_load_score_index(run_dir / "scored.json"), run_dir)
        if scored_items:
            newest = _newest_run_dir()
            is_latest_run = newest is not None and newest.name == run_dir.name
            return {
                "items": scored_items,
                "source": run_dir.name,
                "published": False,
                "note": (
                    "来自最近一次运行的评分结果（导出文件为空）"
                    if is_latest_run
                    else f"本轮无入榜结果 — 显示 {run_dir.name} 的上次有效数据"
                ),
                "count": len(scored_items),
                "stale": not is_latest_run,
            }

    return {
        "items": [],
        "source": "",
        "published": False,
        "note": "还没有测速结果 — 点「开始运行」跑一轮即可看到入榜节点",
        "count": 0,
        "stale": False,
    }


def _merge_scores(items: list[dict], scored: dict[tuple[str, str], dict]) -> list[dict]:
    out = []
    for it in items:
        row = dict(it)
        meta = scored.get((row["address"], str(row["port"]))) or scored.get(
            (row["address"], row["port"])
        )
        if meta:
            if not row.get("score"):
                row["score"] = meta.get("score") or ""
            if meta.get("rank") is not None:
                row["rank"] = meta["rank"]
            if meta.get("region"):
                row["region"] = meta["region"]
        out.append(row)
    return out


def _scored_to_rows(
    scored: dict[tuple[str, str], dict], run_dir: Path
) -> list[dict]:
    """Build table rows from scored.json when no addapi export exists."""
    try:
        data = json.loads((run_dir / "scored.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows: list[dict] = []
    for key in ("endpoints", "proxies"):
        for node in data.get(key) or []:
            if not isinstance(node, dict) or node.get("status") != "ranked":
                continue
            score = node.get("score")
            try:
                score_txt = f"{float(score):.2f}" if score is not None else ""
            except (TypeError, ValueError):
                score_txt = ""
            speed = node.get("speed_mb_s")
            purity = node.get("score_breakdown", {}).get("purity") if isinstance(
                node.get("score_breakdown"), dict
            ) else None
            stability = node.get("availability_rate")
            rows.append(
                {
                    "address": str(node.get("address") or ""),
                    "port": str(node.get("port") or ""),
                    "speed": f"{speed:.1f}" if isinstance(speed, (int, float)) else "",
                    "purity": f"{purity:.2f}" if isinstance(purity, (int, float)) else "",
                    "stability": f"{stability:.2f}" if isinstance(stability, (int, float)) else "",
                    "country": str(node.get("country_code") or "??"),
                    "region": str(node.get("region") or ""),
                    "score": score_txt,
                    "remark": "",
                }
            )
    rows.sort(key=lambda r: (r.get("score") or ""), reverse=True)
    return rows


# ── pool import ─────────────────────────────────────────────────────────
LINE_RE = re.compile(
    r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::\d{1,5})?(?:#.*)?$"
)


def import_pool(text: str) -> int:
    POOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    kept = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        host = line.split("#", 1)[0].strip()
        host = host.split(",")[0].strip()
        if host.startswith("http://") or host.startswith("https://"):
            kept.append(line)
        elif LINE_RE.match(host):
            kept.append(line)
    prev = ""
    if POOL_PATH.is_file():
        prev = POOL_PATH.read_text(encoding="utf-8")
    merged = [x for x in (prev.splitlines() + kept) if x.strip()]
    # unique preserve order
    seen = set()
    out = []
    for x in merged:
        if x not in seen:
            seen.add(x)
            out.append(x)
    POOL_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    return len(kept)


# ── HTTP handler ────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        pass

    def _json(self, obj: dict | list, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode() or "{}")

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/" or path == "/index.html":
            return self._file(WEB / "index.html", "text/html; charset=utf-8")
        if path.endswith(".css"):
            return self._file(WEB / path.lstrip("/"), "text/css; charset=utf-8")
        if path.endswith(".js"):
            return self._file(WEB / path.lstrip("/"), "application/javascript; charset=utf-8")

        if path == "/api/status":
            env = load_env()
            keys = {
                "GITHUB_TOKEN": env.get("GITHUB_TOKEN", ""),
                "ABUSEIPDB_KEY": env.get("ABUSEIPDB_KEY", ""),
                "IPINFO_TOKEN": env.get("IPINFO_TOKEN", ""),
            }
            urls = current_publish_urls()
            cfg = load_config()
            probe_cfg = cfg.get("probe") or {}
            cf_cfg = probe_cfg.get("cf") or {}
            bypass = cf_cfg.get("bypass_system_proxy")
            if bypass is None:
                bypass = True
            return self._json(
                {
                    "ready": any(keys.values()),
                    "keys": keys,
                    "env": [
                        {"name": n, "ok": bool(v)} for n, v in keys.items()
                    ],
                    "github_login": env.get("GITHUB_LOGIN", ""),
                    "github_repo": env.get("GITHUB_REPO", ""),
                    "publish_url": urls["addapi"],
                    "publish_addapi": urls["addapi"],
                    "publish_addcsv": urls["addcsv"],
                    "bypass_system_proxy": bool(bypass),
                    "theme": load_theme(),
                    "diversity": {
                        "enabled": bool(
                            ((cfg.get("scoring") or {}).get("diversity") or {}).get(
                                "enabled", False
                            )
                        ),
                        "per_region": int(
                            ((cfg.get("scoring") or {}).get("diversity") or {}).get(
                                "per_region", 15
                            )
                            or 15
                        ),
                    },
                }
            )
        if path == "/api/run/status":
            return self._json(dict(_state))
        if path == "/api/results":
            return self._json(load_results())
        if path == "/api/scheduler/status":
            return self._json(run_scheduler_cli("status"))
        if path == "/api/theme":
            return self._json({"theme": load_theme()})
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        body = self._read()
        try:
            if path == "/api/github/whoami":
                # Save token first (if provided), then resolve username.
                token = (body.get("GITHUB_TOKEN") or "").strip()
                if token:
                    save_env({"GITHUB_TOKEN": token})
                user = gh_api("/user")
                login = user.get("login", "")
                if login:
                    save_repo_state(login, load_env().get("GITHUB_REPO", ""))
                urls = current_publish_urls()
                return self._json({"ok": True, "login": login, **urls})

            if path == "/api/github/repos":
                # List the authenticated user's public repos (for picker).
                repos = gh_api("/user/repos?per_page=100&sort=updated")
                items = [
                    {
                        "name": r.get("name", ""),
                        "full_name": r.get("full_name", ""),
                        "private": bool(r.get("private")),
                        "updated_at": r.get("updated_at", ""),
                        "html_url": r.get("html_url", ""),
                    }
                    for r in repos
                    if isinstance(r, dict)
                ]
                return self._json({"ok": True, "items": items})

            if path == "/api/repo/select":
                # Use an existing repo — just record it, no create call.
                name = (body.get("name") or "").strip()
                if not name:
                    raise RuntimeError("请提供仓库名")
                login = load_env().get("GITHUB_LOGIN", "").strip()
                if not login:
                    user = gh_api("/user")
                    login = user.get("login", "")
                if login:
                    save_repo_state(login, name)
                urls = repo_raw_urls(name, login=login or None)
                return self._json(
                    {"ok": True, "login": login, "name": name, **urls}
                )

            if path == "/api/repo/create":
                name = (body.get("name") or "cf-ip-pool").strip()
                repo = gh_api(
                    "/user/repos",
                    "POST",
                    {"name": name, "private": False, "auto_init": False},
                )
                login = load_env().get("GITHUB_LOGIN", "").strip()
                if not login:
                    user = gh_api("/user")
                    login = user.get("login", "")
                if login:
                    save_repo_state(login, name)
                urls = repo_raw_urls(name, login=login or None)
                return self._json(
                    {
                        "ok": True,
                        "url": repo.get("html_url", ""),
                        "login": login,
                        "name": name,
                        **urls,
                    }
                )

            if path == "/api/keys":
                save_env(
                    {
                        "GITHUB_TOKEN": body.get("GITHUB_TOKEN", ""),
                        "ABUSEIPDB_KEY": body.get("ABUSEIPDB_KEY", ""),
                        "IPINFO_TOKEN": body.get("IPINFO_TOKEN", ""),
                    }
                )
                return self._json({"ok": True})

            if path == "/api/settings":
                data = load_config()
                scoring = data.setdefault("scoring", {})
                if "weights" in body:
                    scoring["cf_weights"] = body["weights"]
                if "filters" in body:
                    scoring.setdefault("filters", {}).update(body["filters"])
                if "addapi_remark_template" in body:
                    data["addapi_remark_template"] = body["addapi_remark_template"]
                if "bypass_system_proxy" in body:
                    on = bool(body["bypass_system_proxy"])
                    probe = data.setdefault("probe", {})
                    cf = probe.setdefault("cf", {})
                    cf["bypass_system_proxy"] = on
                    proxy = probe.setdefault("proxy", {})
                    proxy["bypass_system_proxy"] = on
                if "diversity" in body and isinstance(body["diversity"], dict):
                    scoring = data.setdefault("scoring", {})
                    div = scoring.setdefault("diversity", {})
                    src = body["diversity"]
                    if "enabled" in src:
                        div["enabled"] = bool(src["enabled"])
                    if "per_region" in src:
                        try:
                            div["per_region"] = max(1, int(src["per_region"]))
                        except (TypeError, ValueError):
                            pass
                save_config(data)
                return self._json({"ok": True})

            if path == "/api/pool/import":
                n = import_pool(body.get("text", ""))
                return self._json({"ok": True, "count": n})

            if path == "/api/pool/search":
                q = body.get("query") or "filename:addressesapi.txt"
                res = gh_api(
                    "/search/code?q=" + urllib.parse.quote(q) + "&per_page=10"
                )
                items = [
                    {
                        "address": it.get("repository", {}).get("full_name", ""),
                        "port": "",
                        "source": "github",
                        "remarks": it.get("path", ""),
                    }
                    for it in res.get("items", [])
                ]
                return self._json({"ok": True, "items": items})

            if path == "/api/pool/subs":
                return self._json({"ok": True, "count": 0, "items": []})

            if path == "/api/autostart-status":
                return self._json({"enabled": autostart_enabled()})

            if path == "/api/autostart":
                enable = bool(body.get("enable"))
                set_autostart(enable)
                return self._json({"ok": True, "enabled": autostart_enabled()})

            if path == "/api/run":
                if _state["running"]:
                    return self._json({"ok": False, "error": "已在运行"}, 409)
                threading.Thread(target=run_worker, daemon=True).start()
                return self._json({"ok": True})

            if path == "/api/theme":
                theme = save_theme(str(body.get("theme") or "light"))
                return self._json({"ok": True, "theme": theme})

            if path == "/api/scheduler/install":
                time_value = str(body.get("time") or "03:00").strip() or "03:00"
                profile = str(body.get("profile") or "auto-collect").strip()
                result = run_scheduler_cli(
                    "install",
                    "--profile",
                    profile,
                    "--time",
                    time_value,
                    "--yes",
                )
                result["time"] = time_value
                result["profile"] = profile
                return self._json(result)

            if path == "/api/scheduler/uninstall":
                result = run_scheduler_cli("uninstall", "--yes")
                return self._json(result)

            if path == "/api/open-output":
                out = _output_base()
                out.mkdir(parents=True, exist_ok=True)
                if os.name == "nt":
                    os.startfile(out)  # noqa: S606
                return self._json({"ok": True})

            self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)

    def _file(self, path: Path, ctype: str) -> None:
        if not path.is_file():
            self.send_response(404)
            self.end_headers()
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_server(host: str = "127.0.0.1", preferred_port: int = PORT) -> int:
    """Bind and serve on a free port (preferred_port, then +1…).

    Returns the port actually bound. Raises OSError when none are free —
    a silent bind failure looks like "connection refused" in the webview.
    """
    last_err: Exception | None = None
    httpd = None
    port = preferred_port
    for candidate in range(preferred_port, preferred_port + 30):
        try:
            httpd = ThreadingHTTPServer((host, candidate), Handler)
            port = candidate
            break
        except OSError as exc:
            last_err = exc
            continue
    if httpd is None:
        _bound["error"] = str(last_err or "no free port")
        raise OSError(f"无法绑定本地端口 {preferred_port}+：{last_err}")
    _bound["port"] = port
    _bound["error"] = ""
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return port


def wait_ready(host: str = "127.0.0.1", port: int = PORT, timeout_s: float = 5.0) -> bool:
    """Poll until the local HTTP server answers, or timeout."""
    import socket
    import time

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main(open_browser: bool = True) -> int:
    port = start_server()
    url = f"http://127.0.0.1:{port}"
    print(f"NodeBench UI → {url}")
    if open_browser:
        threading.Timer(0.4, lambda: __import__("webbrowser").open(url)).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
