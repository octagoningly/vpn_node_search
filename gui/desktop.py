"""NodeBench Desktop — modern web UI in a native window.

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


def main() -> int:
    import webview  # type: ignore

    import server

    # start local API + static server in background
    threading.Thread(target=lambda: server.main(open_browser=False), daemon=True).start()
    # wait until port is up
    time.sleep(0.6)

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
    webview.start(debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
