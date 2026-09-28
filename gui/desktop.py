"""NodeBench Desktop — native window, tray, autostart, dark mode.

Double-click the built exe, or run:  uv run python gui/desktop.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _make_icon():
    """Simple blue rounded-square icon for the tray."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(0, 112, 243, 255))
    d.ellipse((20, 20, 44, 44), outline=(255, 255, 255, 255), width=5)
    d.ellipse((28, 28, 36, 36), fill=(255, 255, 255, 255))
    return img


def main() -> int:
    import webview  # type: ignore

    import server

    threading.Thread(target=lambda: server.main(open_browser=False), daemon=True).start()
    time.sleep(0.6)

    state = {"hidden": False}

    def on_closing(*_args):
        """Hide to tray instead of quitting (unless already hidden)."""
        if state.get("tray_disabled"):
            return True  # allow close
        win.hide()
        state["hidden"] = True
        return False  # block destroy → stay resident

    window = webview.create_window(
        title="NodeBench · 优选节点",
        url="http://127.0.0.1:8765",
        width=1180,
        height=760,
        min_size=(960, 620),
        background_color="#fafafa",
        easy_drag=False,
        text_select=True,
    )
    win = window

    try:
        window.events.closing += on_closing
    except Exception:
        pass

    # ── tray ──────────────────────────────────────
    def show_window(_icon=None, _item=None):
        win.show()
        win.restore()
        state["hidden"] = False

    def quit_app(icon=None, _item=None):
        state["tray_disabled"] = True
        try:
            win.destroy()
        except Exception:
            pass
        if icon:
            icon.stop()

    def open_settings(_icon, _item):
        # jump to keys page via JS
        try:
            win.evaluate_js("document.querySelector('[data-page=keys]')?.click()")
        except Exception:
            pass
        show_window()

    tray = None
    try:
        import pystray
        from pystray import Menu, MenuItem

        tray = pystray.Icon(
            "NodeBench",
            icon=_make_icon(),
            title="NodeBench",
            menu=Menu(
                MenuItem("打开面板", show_window, default=True),
                MenuItem("密钥与设置", open_settings),
                Menu.SEPARATOR,
                MenuItem("退出", quit_app),
            ),
        )
        threading.Thread(target=tray.run, daemon=True).start()
    except Exception:
        tray = None

    webview.start(debug=False)
    if tray:
        try:
            tray.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
