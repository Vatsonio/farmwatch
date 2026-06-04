"""Open the farmwatch settings panel in its own desktop window.

Starts the FastAPI app on a local port in a background thread, then opens a
native window (pywebview, Edge WebView2 on Windows) pointed at it. No browser
needed.

Run:
    python -m web.gui
"""

import socket
import threading
import time

import uvicorn
import webview

from web.server import app

WINDOW_BG = "#0c0e12"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(port: int):
    # uvicorn skips signal handlers when off the main thread, so this is safe.
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def _wait_until_up(port: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.25).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def main():
    port = _free_port()
    threading.Thread(target=_serve, args=(port,), daemon=True).start()
    _wait_until_up(port)
    webview.create_window(
        "farmwatch · settings",
        f"http://127.0.0.1:{port}/",
        width=1240,
        height=880,
        min_size=(920, 640),
        background_color=WINDOW_BG,
    )
    webview.start()


if __name__ == "__main__":
    main()
