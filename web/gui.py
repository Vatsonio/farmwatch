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

import logging
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


def _setup_file_logging():
    """Make the in-process bot and monitor write their log files next to the exe.

    Both modules call logging.basicConfig with a relative path, but in the GUI
    process logging is already configured (so their basicConfig is a no-op) and the
    cwd may differ from the exe. We chdir to the data dir and attach a dedicated
    file handler to each logger so the panel's log viewer and diagnostics see them.
    """
    base = appconfig.base_dir()
    try:
        os.chdir(base)  # so any other relative-path handlers also land next to the exe
    except Exception:
        pass
    # The modules' basicConfig may have put a file handler on the root logger with a
    # relative path; drop it so logs only go where the panel reads them.
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    for name, fname in (("telegram_bot", "telegram_bot.log"),
                        ("printer_monitor", "printer_monitor.log")):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        # Keep each component's lines in its own file only: propagate=False stops them
        # bubbling to a root file handler (which would duplicate every line).
        lg.propagate = False
        for h in list(lg.handlers):
            if isinstance(h, logging.FileHandler):
                lg.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
        try:
            fh = logging.FileHandler(base / fname, encoding="utf-8")
        except Exception:
            continue
        # Pin INFO so that when Debug logging flips the monitor logger to DEBUG, the
        # normal log stays operational only; the verbose DEBUG lines go to the
        # separate printer_monitor.debug.log (otherwise the two logs are identical).
        fh.setLevel(logging.INFO)
        fh.setFormatter(fmt)
        fh._fw = fname
        lg.addHandler(fh)


def _promote_tray_icon():
    """Windows 11 hides new tray icons in the overflow flyout. Flip IsPromoted=1 for
    this executable under NotifyIconSettings so the icon shows in the tray itself.
    Best effort: takes effect from the next launch if the entry is created late."""
    if os.name != "nt":
        return
    try:
        import sys
        import winreg
    except Exception:
        return
    exe = os.path.normcase(os.path.abspath(sys.executable))
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                              r"Control Panel\NotifyIconSettings", 0,
                              winreg.KEY_ALL_ACCESS)
    except OSError:
        return
    try:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                sk = winreg.OpenKey(root, sub, 0, winreg.KEY_ALL_ACCESS)
            except OSError:
                continue
            try:
                try:
                    path, _ = winreg.QueryValueEx(sk, "ExecutablePath")
                except FileNotFoundError:
                    path = ""
                if path and os.path.normcase(os.path.abspath(path)) == exe:
                    winreg.SetValueEx(sk, "IsPromoted", 0, winreg.REG_DWORD, 1)
            finally:
                winreg.CloseKey(sk)
    finally:
        winreg.CloseKey(root)


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


def _run_app():
    """Supervise the in-process Telegram bot so the panel reflects and controls it,
    sharing its monitor for live metrics. The Restart bot button cancels the running
    bot; this loop then tears it down and starts a fresh one. With no token (or if the
    bot cannot run) it falls back to a metrics only monitor."""
    import asyncio

    log = logging.getLogger("gui")
    while True:
        cfg = appconfig.load_config()
        if not appconfig.token_is_set(cfg):
            break  # no token -> metrics only monitor (below)
        try:
            from telegram_bot import BambuTelegramBot, acquire_single_instance_lock
            # Hold the same single instance lock the console bot uses. If a console
            # bot is already running, skip the in-app bot (keep metrics only) so two
            # bots never poll Telegram at once.
            if not acquire_single_instance_lock():
                log.warning("another farmwatch bot holds the lock; running metrics only")
                break
        except Exception as e:
            log.error("cannot start in-app bot: %s", e)
            break

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        bot = BambuTelegramBot()
        app.state.bot = bot
        app.state.bot_loop = loop
        app.state.bot_restart = False

        def _share(b=bot):
            for _ in range(240):
                m = getattr(b, "monitor", None)
                if m is not None:
                    app.state.monitor = m
                    return
                time.sleep(0.5)

        threading.Thread(target=_share, daemon=True).start()

        task = loop.create_task(bot.start())
        app.state.bot_task = task
        try:
            loop.run_until_complete(task)  # blocks until the bot stops or is cancelled
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("in-app bot stopped: %s", e)
        finally:
            try:
                loop.run_until_complete(bot.stop())
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            app.state.bot = None
            app.state.bot_loop = None
            app.state.bot_task = None

        if not app.state.bot_restart:
            return
        log.info("restarting the in-app bot...")
        time.sleep(1.0)  # let the old polling fully release before reconnecting

    # No token, or the bot cannot run: keep just the monitor for metrics.
    try:
        if app.state.monitor is None:
            app.state.monitor = _start_monitor()
    except Exception:
        pass


def main():
    _setup_file_logging()
    app.state.app_runner = _run_app  # lets /api/bot/restart relaunch the bot supervisor
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
        def _setup(icon):
            icon.visible = True
            # Ask Windows to keep the icon out of the hidden overflow.
            threading.Thread(target=_promote_tray_icon, daemon=True).start()

        icon = pystray.Icon(
            "farmwatch", make_tray_image(64), "farmwatch",
            menu=pystray.Menu(
                pystray.MenuItem("Open", on_open, default=True),
                pystray.MenuItem("Quit", on_quit),
            ),
        )
        icon.run(setup=_setup)

    def on_started():
        threading.Thread(target=run_tray, daemon=True).start()
        # Start the bot + monitor in the background so the window opens instantly
        # instead of blocking on the Telegram and CDP connects during startup.
        threading.Thread(target=_run_app, daemon=True).start()

    webview.start(on_started)
    # webview.start returns once the window is destroyed (Quit). Force a full process
    # exit so no lingering thread keeps the app (or the tray icon) alive.
    os._exit(0)


if __name__ == "__main__":
    main()
