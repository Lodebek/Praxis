"""Praxis launcher: starts the local server and opens your browser.

Usage:  python run.py
"""

from __future__ import annotations

import threading
import webbrowser

import uvicorn

from praxis.config import load_config


def main() -> None:
    cfg = load_config()
    srv = cfg.get("server") or {}
    host = srv.get("host", "127.0.0.1")
    port = int(srv.get("port", 8765))

    if srv.get("open_browser", True):
        url = f"http://{host}:{port}"
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    print(f"\n  Praxis running at  http://{host}:{port}\n  Press Ctrl+C to stop.\n")
    # reload=True so editing code while it's running picks up automatically
    # (avoids a stale server serving fresh static files but old API routes).
    uvicorn.run("praxis.server:app", host=host, port=port, log_level="info",
                reload=bool(srv.get("reload", True)))


if __name__ == "__main__":
    main()
