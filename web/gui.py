"""farmwatch tray app.

On launch the app sits in the system tray. Click the tray icon (or the Open
menu item) to open the window with live farm metrics and settings. Closing the
window with the X hides it back to the tray; Quit in the tray menu exits.

It runs the printer monitor in the background (connecting to an already running
Bambu client with the debug port) for live metrics, and serves the panel via
the embedded FastAPI app.

Run:
    python -m web.gui
"""

import os
import socket
import threading
import time

import uvicorn
import webview
import pystray

import appconfig
from version import __version__
from web.icon import make_tray_image
from web.server import app
from printer_monitor import BambuPrinterMonitor, BAMBU_EXE_PATH

WINDOW_BG = "#0c0e12"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_up(port: int, timeout: float = 8.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.25).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def _start_monitor():
    """Connect to an already running debug client (no relaunch) for live metrics."""
    cfg = appconfig.load_config().get("monitor", {})
    mon = BambuPrinterMonitor(
        debug_port=cfg.get("debug_port", 9222),
        update_interval=cfg.get("update_interval", 30),
        exe_path=cfg.get("exe_path", BAMBU_EXE_PATH),
        auto_launch=False,
        debug_logging=cfg.get("debug_logging", False),
    )
    try:
        if mon.is_app_running():
            mon.start()
    except Exception:
        pass
    return mon


def main():
    appconfig.ensure_config()
    port = _free_port()
    # use_colors=False + log_config=None: in a --windowed frozen exe sys.stdout is
    # None, and uvicorn's default log formatter calls sys.stdout.isatty() and crashes.
    threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=port,
                                   log_level="warning", use_colors=False, log_config=None),
        daemon=True,
    ).start()
    _wait_up(port)
    url = f"http://127.0.0.1:{port}/"

    window = webview.create_window(
        f"FarmWatch v{__version__}", url, width=1240, height=880, min_size=(900, 640),
        background_color=WINDOW_BG, hidden=True,
    )

    state = {"quitting": False}

    def on_open(icon=None, item=None):
        window.show()

    def on_quit(icon, item):
        state["quitting"] = True
        try:
            if app.state.monitor:
                app.state.monitor.stop()
        except Exception:
            pass
        try:
            icon.stop()
        except Exception:
            pass
        try:
            window.destroy()
        except Exception:
            pass

    def on_closing():
        # The X hides to the tray; only a real Quit is allowed to close the window.
        if state["quitting"]:
            return True
        window.hide()
        return False

    window.events.closing += on_closing

    def run_tray():
        icon = pystray.Icon(
            "farmwatch", make_tray_image(64), "farmwatch",
            menu=pystray.Menu(
                pystray.MenuItem("Open", on_open, default=True),
                pystray.MenuItem("Quit", on_quit),
            ),
        )
        icon.run()

    def on_started():
        threading.Thread(target=run_tray, daemon=True).start()
        # Connect the metrics monitor in the background so the window opens instantly
        # instead of blocking ~7s on the CDP connect during startup.
        threading.Thread(target=lambda: setattr(app.state, "monitor", _start_monitor()),
                         daemon=True).start()

    webview.start(on_started)
    # webview.start returns once the window is destroyed (Quit). Force a full process
    # exit so no lingering thread keeps the app (or the tray icon) alive.
    os._exit(0)


if __name__ == "__main__":
    main()
