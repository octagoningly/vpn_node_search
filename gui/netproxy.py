"""Windows system-proxy aware HTTP GET/POST via urllib.

Clashes / system proxies are common in CN; raw urllib does not use them
by default and TLS handshake then fails (SSL UNEXPECTED_EOF).
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_UA = "NodeBench/0.1"


def windows_system_proxies() -> dict[str, str]:
    """Read IE/WinINET proxy settings from the registry (Windows only)."""
    if os.name != "nt":
        return {}
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0,
            winreg.KEY_READ,
        )
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        except OSError:
            return {}
        finally:
            winreg.CloseKey(key)
        if not enable or not server:
            return {}
        server = str(server).strip()
        # "host:port" or "http=h:p;https=h:p"
        if "=" in server and ";" in server:
            out = {}
            for part in server.split(";"):
                if "=" in part:
                    k, _, v = part.partition("=")
                    out[k.strip().lower()] = f"http://{v.strip()}"
            return out
        return {"http": f"http://{server}", "https": f"http://{server}"}
    except Exception:
        return {}


def build_opener() -> urllib.request.OpenerDirector:
    proxies = urllib.request.getproxies()
    proxies.update(windows_system_proxies())
    handlers: list[Any] = []
    if proxies:
        handlers.append(urllib.request.ProxyHandler(proxies))
    # Some local proxies need SNI/legacy compatibility; keep default SSL first.
    ctx = ssl.create_default_context()
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


_OPENER = None


def get_opener() -> urllib.request.OpenerDirector:
    global _OPENER
    if _OPENER is None:
        _OPENER = build_opener()
    return _OPENER


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict | None = None,
    timeout: float = 25.0,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode()
    hdrs = {"User-Agent": DEFAULT_UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with get_opener().open(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(text)
        except Exception as err:
            raise RuntimeError(f"HTTP {exc.code}: {text[:200]}") from err
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络错误: {exc.reason}") from exc
    except OSError as exc:
        raise RuntimeError(f"网络错误: {exc}") from exc
    return json.loads(body.decode("utf-8", "replace"))
