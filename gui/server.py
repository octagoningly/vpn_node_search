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
import urllib.error
import urllib.parse
import urllib.request
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
PORT = 8765

# run state
_state = {
    "running": False,
    "log": "",
    "ok": False,
    "code": None,
    "stage_index": -1,
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
def run_worker() -> None:
    _state.update(running=True, log="启动 nodebench run --profile auto-collect…\n", stage_index=0)
    try:
        proc = subprocess.run(
            ["uv", "run", "nodebench", "run", "--profile", "auto-collect"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=(os.name == "nt"),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        _state["log"] += out[-12000:]
        _state["code"] = proc.returncode
        _state["ok"] = proc.returncode == 0
        _state["stage_index"] = 4 if proc.returncode == 0 else 2
    except Exception as exc:  # noqa: BLE001
        _state["log"] += f"\n启动失败: {exc}\n"
        _state["code"] = -1
        _state["ok"] = False
    finally:
        _state["running"] = False


# ── results ─────────────────────────────────────────────────────────────
def load_results() -> list[dict]:
    exp = ROOT / "output" / "latest" / "cf-addapi.txt"
    if not exp.is_file():
        return []
    items = []
    for line in exp.read_text(encoding="utf-8").splitlines():
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
            }
        )
    return items


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
                }
            )
        if path == "/api/run/status":
            return self._json(dict(_state))
        if path == "/api/results":
            return self._json({"items": load_results()})
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

            if path == "/api/repo/create":
                name = (body.get("name") or "cf-ip-pool").strip()
                repo = gh_api(
                    "/user/repos",
                    "POST",
                    {"name": name, "private": False, "auto_init": False},
                )
                urls = repo_raw_urls(name)
                return self._json(
                    {"ok": True, "url": repo.get("html_url", ""), **urls}
                )

            if path == "/api/settings":
                data = load_config()
                scoring = data.setdefault("scoring", {})
                if "weights" in body:
                    scoring["cf_weights"] = body["weights"]
                if "filters" in body:
                    scoring.setdefault("filters", {}).update(body["filters"])
                if "addapi_remark_template" in body:
                    data["addapi_remark_template"] = body["addapi_remark_template"]
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

            if path == "/api/open-output":
                out = ROOT / "output"
                out.mkdir(exist_ok=True)
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


def main(open_browser: bool = True) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"NodeBench UI → {url}")
    if open_browser:
        threading.Timer(0.4, lambda: __import__("webbrowser").open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
