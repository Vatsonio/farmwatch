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
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("web")

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).parent / "static"
CONFIG = ROOT / "config.json"
EXAMPLE = ROOT / "config.example.json"
BOT_LOCK_PORT = 48217  # the single instance port telegram_bot.py binds while running

LOG_FILES = {
    "bot": ROOT / "telegram_bot.log",
    "monitor": ROOT / "printer_monitor.log",
    "debug": ROOT / "printer_monitor.debug.log",
}

app = FastAPI(title="farmwatch settings")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ----------------------------- config io -----------------------------
def load_config() -> dict:
    path = CONFIG if CONFIG.exists() else EXAMPLE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("could not read %s: %s", path, e)
        return {}


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
        "exe_default": str(EXAMPLE.exists()),
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


@app.get("/api/status")
async def api_status():
    cfg = load_config()
    tg = cfg.get("telegram", {})
    mon = cfg.get("monitor", {})
    dumps_dir = ROOT / "debug_dumps"
    dumps = sorted([p.name for p in dumps_dir.glob("*.html")]) if dumps_dir.exists() else []
    return {
        "version": read_version(),
        "bot_running": bot_running(),
        "token_set": bool(tg.get("bot_token") and "PUT-YOUR" not in str(tg.get("bot_token"))),
        "users": len(tg.get("allowed_users", [])),
        "groups": len(tg.get("allowed_groups", [])),
        "debug_logging": bool(mon.get("debug_logging", False)),
        "update_interval": mon.get("update_interval"),
        "auto_launch": bool(mon.get("auto_launch", True)),
        "proxy_enabled": bool(tg.get("proxy", {}).get("enabled", False)),
        "dumps": len(dumps),
        "config_exists": CONFIG.exists(),
    }


@app.get("/api/logs")
async def api_logs(name: str = "monitor", tail: int = 200):
    path = LOG_FILES.get(name)
    if not path or not path.exists():
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
    mon_log = LOG_FILES["monitor"]
    hits = []
    if mon_log.exists():
        try:
            for ln in mon_log.read_text(encoding="utf-8", errors="replace").splitlines():
                if ("ЗНИКЛИ" in ln) or ("0 карток" in ln) or ("впала" in ln):
                    hits.append(ln)
        except Exception as e:
            hits.append(f"(could not read monitor log: {e})")
    dumps_dir = ROOT / "debug_dumps"
    dumps = sorted([p.name for p in dumps_dir.glob("*.html")], reverse=True) if dumps_dir.exists() else []
    return {"disappearances": hits[-tail:], "dump_count": len(dumps), "dumps": dumps[:40]}


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
