"""Shared config helpers: where config.json lives and auto-creating it.

config.json is kept next to the executable (the folder you run the exe from)
when frozen by PyInstaller, or in the current directory when run from source.
If it does not exist yet it is created from DEFAULT_CONFIG, so both the console
bot and the GUI work on first launch with no manual file setup.
"""

import json
import sys
from pathlib import Path

DEFAULT_CONFIG = {
    "telegram": {
        "bot_token": "PUT-YOUR-BOT-TOKEN-HERE",
        "allowed_users": [],
        "allowed_groups": [],
        "proxy": {
            "enabled": False,
            "type": "socks5",
            "host": "127.0.0.1",
            "port": 1080,
            "username": "",
            "password": "",
        },
    },
    "monitor": {
        "debug_port": 9222,
        "update_interval": 60,
        "auto_launch": True,
        "exe_path": "C:\\Program Files\\Bambu Farm Manager Client\\Bambu Farm Manager Client.exe",
        "debug_logging": False,
    },
    "notifications": {
        "status_changes": True,
        "print_complete": True,
        "printer_offline": True,
        "printer_online": False,
        "periodic_updates": False,
        "periodic_interval": 3600,
    },
    "serial": {
        "enabled": False,
        "port": "auto",
        "baud": 115200,
        "labels": "numbers",
    },
}


def base_dir() -> Path:
    """Folder the app should read/write config from: next to the exe when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def config_path() -> Path:
    return base_dir() / "config.json"


def token_is_set(cfg: dict) -> bool:
    tok = (cfg or {}).get("telegram", {}).get("bot_token", "") or ""
    return bool(tok) and "PUT-YOUR" not in tok


def ensure_config() -> Path:
    """Create config.json from defaults if it is missing. Returns its path."""
    p = config_path()
    if not p.exists():
        try:
            p.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return p


def load_config() -> dict:
    """Ensure config.json exists, then load it (falling back to defaults)."""
    p = ensure_config()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_CONFIG)
