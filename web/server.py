"""farmwatch v2 settings GUI (FastAPI backend).

A local control panel for the farmwatch program: read and write config.json,
show program status, tail logs, and surface the printer disappearance
diagnostics. It does NOT re-create the Bambu printer dashboard.

Run:
    python -m web.server            # http://127.0.0.1:8000
"""

import json
import logging
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import appconfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("web")


def _static_dir() -> Path:
    # PyInstaller bundles web/static into _MEIPASS via --add-data.
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", ".")) / "web" / "static"
    return Path(__file__).parent / "static"


ROOT = appconfig.base_dir()          # logs, dumps and config live next to the exe
STATIC = _static_dir()
CONFIG = appconfig.config_path()
BOT_LOCK_PORT = 48217  # the single instance port telegram_bot.py binds while running

LOG_FILES = {
    "bot": ROOT / "telegram_bot.log",
    "monitor": ROOT / "printer_monitor.log",
    "debug": ROOT / "printer_monitor.debug.log",
}

app = FastAPI(title="farmwatch settings")
app.state.monitor = None  # the tray app (web.gui) sets this for live farm metrics
app.state.bot = None      # the tray app sets this when it runs the bot in-process
app.state.bot_loop = None    # the in-process bot's event loop
app.state.bot_task = None    # the running bot.start() task (cancel to restart it)
app.state.bot_restart = False
app.state.bot_enabled = True  # user toggle: when False the supervisor keeps the bot off
app.state.app_runner = None  # web.gui._run_app, to relaunch the bot supervisor
app.state.diag_cleared = set()  # diagnostics lines the user acknowledged via Clear
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# ----------------------------- config io -----------------------------
def load_config() -> dict:
    """Ensure config.json exists next to the exe, then load it."""
    return appconfig.load_config()


def _as_int_list(v):
    out = []
    for x in v or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def coerce_config(data: dict) -> dict:
    """Coerce known fields to their proper types so the saved config stays valid."""
    cfg = dict(data or {})

    tg = dict(cfg.get("telegram", {}))
    tg["bot_token"] = str(tg.get("bot_token", "") or "")
    tg["allowed_users"] = _as_int_list(tg.get("allowed_users", []))
    tg["allowed_groups"] = _as_int_list(tg.get("allowed_groups", []))
    px = dict(tg.get("proxy", {}))
    px["enabled"] = bool(px.get("enabled", False))
    px["type"] = str(px.get("type", "socks5") or "socks5")
    px["host"] = str(px.get("host", "127.0.0.1") or "127.0.0.1")
    try:
        px["port"] = int(px.get("port", 1080))
    except (TypeError, ValueError):
        px["port"] = 1080
    px["username"] = str(px.get("username", "") or "")
    px["password"] = str(px.get("password", "") or "")
    tg["proxy"] = px
    cfg["telegram"] = tg

    mon = dict(cfg.get("monitor", {}))
    for k, default in (("debug_port", 9222), ("update_interval", 60)):
        try:
            mon[k] = int(mon.get(k, default))
        except (TypeError, ValueError):
            mon[k] = default
    mon["auto_launch"] = bool(mon.get("auto_launch", True))
    mon["debug_logging"] = bool(mon.get("debug_logging", False))
    mon["exe_path"] = str(mon.get("exe_path", "") or "")
    cfg["monitor"] = mon

    nf = dict(cfg.get("notifications", {}))
    for k in ("status_changes", "print_complete", "printer_offline", "printer_online", "periodic_updates"):
        nf[k] = bool(nf.get(k, False))
    try:
        nf["periodic_interval"] = int(nf.get("periodic_interval", 3600))
    except (TypeError, ValueError):
        nf["periodic_interval"] = 3600
    cfg["notifications"] = nf

    sr = dict(cfg.get("serial", {}))
    sr["enabled"] = bool(sr.get("enabled", False))
    sr["port"] = str(sr.get("port", "auto") or "auto")
    try:
        sr["baud"] = int(sr.get("baud", 115200))
    except (TypeError, ValueError):
        sr["baud"] = 115200
    sr["labels"] = "names" if str(sr.get("labels", "numbers")).lower() == "names" else "numbers"
    cfg["serial"] = sr

    return cfg


def bot_running() -> bool:
    """The bot holds 127.0.0.1:48217 while running; a failed bind means it is up."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", BOT_LOCK_PORT))
        return False
    except OSError:
        return True
    finally:
        s.close()


def bot_is_running() -> bool:
    """Truthful bot state. In the tray GUI the bot runs in this process, so its task
    is authoritative; the lock-port heuristic can false-positive (held during startup
    or a lingering lock). Without a task, no token means the bot cannot run at all."""
    task = getattr(app.state, "bot_task", None)
    if task is not None:
        return not task.done()
    if not getattr(app.state, "bot_enabled", True):
        return False
    if not appconfig.token_is_set(load_config()):
        return False
    return bot_running()  # a separate console bot may hold the lock


def esp_connected() -> bool:
    """True when the attached display actually has its serial port open."""
    mon = getattr(app.state, "monitor", None)
    disp = getattr(mon, "_serial_display", None) if mon else None
    return bool(disp is not None and getattr(disp, "_ser", None) is not None)


def diag_hits() -> list:
    """Printer disappearance signals from the monitor log."""
    hits = []
    mon_log = LOG_FILES["monitor"]
    if mon_log.exists():
        try:
            for ln in mon_log.read_text(encoding="utf-8", errors="replace").splitlines():
                if ("ЗНИКЛИ" in ln) or ("0 карток" in ln) or ("впала" in ln):
                    hits.append(ln)
        except Exception as e:
            hits.append(f"(could not read monitor log: {e})")
    return hits


def read_version() -> str:
    try:
        import importlib
        import version as _v

        importlib.reload(_v)
        return getattr(_v, "__version__", "unknown")
    except Exception:
        return "unknown"


# ----------------------------- endpoints -----------------------------
@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
async def api_get_config():
    return JSONResponse({
        "config": load_config(),
        "exists": CONFIG.exists(),
    })


@app.post("/api/config")
async def api_save_config(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)

    cfg = coerce_config(body.get("config", body))
    try:
        if CONFIG.exists():
            shutil.copy2(CONFIG, CONFIG.with_suffix(".json.bak"))
        CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.error("save failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    log.info("config.json saved via settings GUI")
    return {"ok": True, "saved_at": datetime.now().isoformat(), "config": cfg}


@app.get("/api/serial-ports")
async def api_serial_ports():
    """Список COM портів для вибору ESP у налаштуваннях (з позначкою якого порту ESP)."""
    ports = []
    detected = None
    try:
        import serial_output
        from serial.tools import list_ports

        detected = serial_output.autodetect_port()
        for p in list_ports.comports():
            ports.append({
                "device": p.device,
                "description": (p.description or "").strip(),
                "is_esp": getattr(p, "vid", None) in serial_output._ESPRESSIF_VIDS,
            })
    except Exception as e:
        return JSONResponse({"ports": [], "detected": None, "error": str(e)})
    return JSONResponse({"ports": ports, "detected": detected})


@app.get("/api/status")
async def api_status():
    cfg = load_config()
    tg = cfg.get("telegram", {})
    mon = cfg.get("monitor", {})
    dumps_dir = ROOT / "debug_dumps"
    dumps = sorted([p.name for p in dumps_dir.glob("*.html")]) if dumps_dir.exists() else []
    sr = cfg.get("serial", {})
    return {
        "version": read_version(),
        "bot_running": bot_is_running(),
        "bot_enabled": bool(getattr(app.state, "bot_enabled", True)),
        "token_set": bool(tg.get("bot_token") and "PUT-YOUR" not in str(tg.get("bot_token"))),
        "users": len(tg.get("allowed_users", [])),
        "groups": len(tg.get("allowed_groups", [])),
        "debug_logging": bool(mon.get("debug_logging", False)),
        "update_interval": mon.get("update_interval"),
        "auto_launch": bool(mon.get("auto_launch", True)),
        "proxy_enabled": bool(tg.get("proxy", {}).get("enabled", False)),
        "serial_enabled": bool(sr.get("enabled", False)),
        "esp_connected": esp_connected(),
        "dumps": len(dumps),
        "config_exists": CONFIG.exists(),
    }


@app.get("/api/logs")
async def api_logs(name: str = "monitor", tail: int = 200):
    path = LOG_FILES.get(name)
    if not path or not path.exists():
        if name == "debug":
            return PlainTextResponse(
                "(verbose monitor log is empty)\n\n"
                "Turn on Debug logging in the Monitor settings to capture the detailed, "
                "step by step monitor trace here. The normal Monitor log stays concise; "
                "this one adds the low level scraping and printer disappearance detail.")
        return PlainTextResponse("(log file not found)")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return PlainTextResponse(f"(could not read log: {e})")
    tail = max(1, min(tail, 2000))
    return PlainTextResponse("\n".join(lines[-tail:]))


@app.get("/api/diagnostics")
async def api_diagnostics(tail: int = 60):
    """Recent printer disappearance signals from the monitor log + dump list."""
    cleared = getattr(app.state, "diag_cleared", set())
    hits = [h for h in diag_hits() if h not in cleared]
    dumps_dir = ROOT / "debug_dumps"
    dumps = sorted([p.name for p in dumps_dir.glob("*.html")], reverse=True) if dumps_dir.exists() else []
    return {"disappearances": hits[-tail:], "dump_count": len(dumps), "dumps": dumps[:40]}


@app.post("/api/diagnostics/clear")
async def api_diagnostics_clear():
    """Acknowledge (hide) the current disappearance signals. New ones still show."""
    app.state.diag_cleared = set(diag_hits())
    return {"ok": True, "cleared": len(app.state.diag_cleared)}


@app.get("/api/metrics")
async def api_metrics():
    """Live farm metrics from the monitor (used by the tray window)."""
    m = getattr(app.state, "monitor", None)
    if not m or not getattr(m, "running", False):
        return {"connected": False, "summary": {}, "active": []}
    try:
        active = []
        for p in m.get_all_printers():
            st = str(p.status).lower()
            if st in ("printing", "paused", "finished"):
                active.append({
                    "name": p.name,
                    "model": getattr(p, "model", ""),
                    "status": st,
                    "progress": p.progress,
                    "remaining_time": p.remaining_time,
                    "file": p.current_file,
                    "nozzle": p.nozzle_temp,
                    "bed": p.bed_temp,
                    "speed": getattr(p, "speed", ""),
                    "message": getattr(p, "message", "") or "",
                })
        # order: printing, then paused, then finished; within each highest progress first
        _order = {"printing": 0, "paused": 1, "finished": 2}
        active.sort(key=lambda x: (_order.get(x["status"], 9),
                                   x["progress"] is None, -(x["progress"] or 0)))
        return {"connected": True, "summary": m.get_summary(), "active": active}
    except Exception as e:
        return {"connected": False, "error": str(e), "summary": {}, "active": []}


def _build_monitor():
    from printer_monitor import BambuPrinterMonitor, BAMBU_EXE_PATH
    cfg = load_config().get("monitor", {})
    return BambuPrinterMonitor(
        debug_port=cfg.get("debug_port", 9222),
        update_interval=cfg.get("update_interval", 30),
        exe_path=cfg.get("exe_path", BAMBU_EXE_PATH),
        auto_launch=False,
        debug_logging=bool(cfg.get("debug_logging", False)),
    )


@app.post("/api/monitor/restart")
async def api_monitor_restart():
    """Reconnect the live metrics monitor (recovers a stale or 0 card state).

    Reconnect the SAME monitor object in place rather than building a new one, so the
    in-process bot keeps its reference and we never end up with two monitors competing
    for the Bambu debug port.
    """
    import asyncio
    loop = asyncio.get_running_loop()
    mon = app.state.monitor
    if mon is None:
        mon = _build_monitor()
        try:
            ok = await loop.run_in_executor(None, mon.start)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        app.state.monitor = mon
        return {"ok": bool(ok), "running": getattr(mon, "running", False),
                "printers": len(mon.get_all_printers())}
    try:
        await loop.run_in_executor(None, mon.stop)
        ok = await loop.run_in_executor(None, mon.start)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": bool(ok), "running": getattr(mon, "running", False),
            "printers": len(mon.get_all_printers())}


@app.post("/api/bot/restart")
async def api_bot_restart():
    """Restart the Telegram bot running in-process (reloads config: token, proxy,
    allowed users, notifications). If it is not running yet, start the supervisor."""
    loop = getattr(app.state, "bot_loop", None)
    task = getattr(app.state, "bot_task", None)
    if loop is not None and task is not None:
        app.state.bot_restart = True
        try:
            loop.call_soon_threadsafe(task.cancel)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        return {"ok": True, "action": "restarting"}

    # Not running in-process: try to (re)launch the bot supervisor.
    runner = getattr(app.state, "app_runner", None)
    if runner is None:
        return JSONResponse(
            {"ok": False, "error": "bot is not running in this process"},
            status_code=409,
        )
    cfg = load_config()
    if not appconfig.token_is_set(cfg):
        return JSONResponse(
            {"ok": False, "error": "no bot token set"}, status_code=409)
    import threading
    threading.Thread(target=runner, daemon=True).start()
    return {"ok": True, "action": "starting"}


@app.post("/api/bot/stop")
async def api_bot_stop():
    """Turn the in-process bot off (it stays off until started again)."""
    app.state.bot_enabled = False
    loop = getattr(app.state, "bot_loop", None)
    task = getattr(app.state, "bot_task", None)
    if loop is not None and task is not None:
        try:
            loop.call_soon_threadsafe(task.cancel)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "running": False}


@app.post("/api/bot/start")
async def api_bot_start():
    """Turn the in-process bot on (starts the supervisor if needed)."""
    app.state.bot_enabled = True
    if getattr(app.state, "bot", None) is not None:
        return {"ok": True, "running": True}  # already running
    runner = getattr(app.state, "app_runner", None)
    if runner is None:
        return JSONResponse({"ok": False, "error": "GUI runner unavailable"}, status_code=409)
    cfg = load_config()
    if not appconfig.token_is_set(cfg):
        return JSONResponse({"ok": False, "error": "no bot token set"}, status_code=409)
    import threading
    threading.Thread(target=runner, daemon=True).start()
    return {"ok": True, "running": True}


@app.post("/api/bot/test")
async def api_bot_test():
    """Send a test message to the allowed chats to verify Telegram delivery."""
    bot = getattr(app.state, "bot", None)
    loop = getattr(app.state, "bot_loop", None)
    if bot is None or loop is None:
        return JSONResponse({"ok": False, "error": "bot is not running"}, status_code=409)
    import asyncio
    try:
        fut = asyncio.run_coroutine_threadsafe(
            bot.send_notification("✅ farmwatch: тестове повідомлення (Send test message)"),
            loop,
        )
        fut.result(timeout=15)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True}


@app.post("/api/open-folder")
async def api_open_folder():
    """Open the data folder (config.json + logs) in the file manager."""
    try:
        import os
        os.startfile(str(ROOT))  # Windows Explorer at the data dir
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True}


def main():
    import argparse
    import uvicorn

    ap = argparse.ArgumentParser(description="farmwatch settings GUI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    log.info("farmwatch settings on http://%s:%d", a.host, a.port)
    uvicorn.run(app, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
