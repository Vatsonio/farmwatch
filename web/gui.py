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

import json
import socket
import threading
import time

import uvicorn
import webview
import pystray
from PIL import Image, ImageDraw

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
    try:
        cfg = json.load(open("config.json", encoding="utf-8")).get("monitor", {})
    except Exception:
        cfg = {}
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


def make_tray_image(size: int = 64) -> Image.Image:
    """farmwatch tray icon: a brass filament spool seen head-on that doubles as a
    watching eye/target. A solid brass flange ring surrounds a dark moat and a
    floating brass hub (the pupil / spool core). Everything is a fraction of size,
    rendered at 4x and LANCZOS-downsampled, so the bold silhouette stays crisp at
    16x16 and 32x32 in the tray and still reads at 64px for the window icon.
    """
    ss = 4  # supersample for clean antialiased edges at tiny sizes
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    brass = (217, 154, 78, 255)     # #d99a4e  primary accent
    brass_lt = (232, 173, 99, 255)  # #e8ad63  highlight
    field = (12, 14, 18, 255)       # #0c0e12  near-black moat

    c = S / 2.0

    def circle(cx, cy, r, fill=None, outline=None, width=1):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill,
                  outline=outline, width=int(round(width)))

    R = S * 0.46        # outer brass disc radius (spool flange edge)
    ring_w = S * 0.135  # flange thickness
    hub_r = S * 0.135   # central pupil / spool hub radius

    circle(c, c, R, fill=brass)            # solid brass outer disc
    circle(c, c, R - ring_w, fill=field)   # dark filament gap -> bold ring
    circle(c, c, hub_r, fill=brass)        # brass hub floating in the moat

    hw = ring_w * 0.5
    arc_r = R - ring_w * 0.5
    d.arc([c - arc_r, c - arc_r, c + arc_r, c + arc_r],
          start=200, end=315, fill=brass_lt, width=int(round(hw)))

    cl_r = hub_r * 0.42
    cl_off = hub_r * 0.30
    circle(c - cl_off, c - cl_off, cl_r, fill=brass_lt)

    return img.resize((size, size), Image.LANCZOS)


def main():
    port = _free_port()
    app.state.monitor = _start_monitor()
    threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning"),
        daemon=True,
    ).start()
    _wait_up(port)
    url = f"http://127.0.0.1:{port}/"

    window = webview.create_window(
        "farmwatch", url, width=1240, height=880, min_size=(900, 640),
        background_color=WINDOW_BG, hidden=True,
    )

    def on_open(icon=None, item=None):
        window.show()

    def on_quit(icon, item):
        try:
            if app.state.monitor:
                app.state.monitor.stop()
        except Exception:
            pass
        try:
            icon.stop()
        except Exception:
            pass
        window.destroy()

    def on_closing():
        # The X hides to the tray instead of quitting the app.
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

    webview.start(on_started)


if __name__ == "__main__":
    main()
