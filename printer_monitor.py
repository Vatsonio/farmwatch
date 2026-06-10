"""
Bambu Farm Manager - Autonomous Printer Monitor
Постійний моніторинг статусу принтерів з можливістю інтеграції з Telegram Bot
"""

import requests
import websocket
import json
import time
import threading
import subprocess
import os
import re
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, asdict, replace
from bs4 import BeautifulSoup
import logging

# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('printer_monitor.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Шлях до Bambu Farm Manager Client
BAMBU_EXE_PATH = r"C:\Program Files\Bambu Farm Manager Client\Bambu Farm Manager Client.exe"

# Mapping the farm server's devices2 JSON fields to farmwatch's vocabulary.
# gcode_state -> our status (the client reports the print state precisely here).
_GCODE_STATE = {
    "RUNNING": "printing", "PREPARE": "printing", "PREPARING": "printing",
    "SLICING": "printing", "RESUMING": "printing",
    "PAUSE": "paused", "PAUSED": "paused", "PAUSING": "paused",
    "FINISH": "finished", "FINISHED": "finished",
    "FAILED": "stopped", "STOPPED": "stopped",
    "IDLE": "idle",
}
# spd_lvl -> speed label
_SPEED_LVL = {1: "Silent", 2: "Standard", 3: "Sport", 4: "Ludicrous"}
# dev_model code -> friendly model (fallback when the name has no parenthesised model)
_MODEL_CODES = {
    "N1": "A1 mini", "N2S": "A1", "C11": "P1P", "C12": "P1S",
    "C13": "X1", "BL-P001": "X1C", "O1": "H2D",
}


@dataclass
class PrinterStatus:
    """Статус принтера"""
    name: str
    model: str
    status: str  # printing, idle, offline
    progress: int  # 0-100
    current_file: Optional[str]
    remaining_time: Optional[str]  # формат: "-7h11m"
    nozzle_temp: str  # "250/250°C"
    bed_temp: str  # "70/70°C"
    speed: str  # "Standard"
    online: bool
    last_update: datetime
    message: str = ""  # optional alert / reason shown on the card (e.g. HMS warning)
    serial: str = ""             # dev_id from the JSON API
    layer: int = 0               # current layer (JSON only)
    total_layers: int = 0        # total layers (JSON only)
    
    def to_dict(self) -> dict:
        """Конвертація у словник"""
        data = asdict(self)
        data['last_update'] = self.last_update.isoformat()
        return data
    
    def to_telegram_message(self) -> str:
        """Форматування для Telegram повідомлення"""
        status_emoji = {
            'printing': '🖨️',
            'idle': '⏸️',
            'offline': '❌',
            'finished': '✅',
            'stopped': '🛑',
            'paused': '⏸️'
        }
        
        emoji = status_emoji.get(self.status, '❓')
        msg = f"{emoji} **{self.name}** ({self.model})\n"
        msg += f"📊 Status: {self.status.upper()}\n"
        
        if self.status == 'printing':
            msg += f"📄 File: {self.current_file}\n"
            msg += f"📈 Progress: {self.progress}%\n"
            msg += f"⏱️ Remaining: {self.remaining_time}\n"
            msg += f"🌡️ Nozzle: {self.nozzle_temp}\n"
            msg += f"🔥 Bed: {self.bed_temp}\n"
            msg += f"⚡ Speed: {self.speed}\n"
        elif self.status == 'paused':
            if self.current_file:
                msg += f"📄 File: {self.current_file}\n"
            msg += f"📈 Progress: {self.progress}%\n"
            msg += f"⏸️ Print paused\n"
        elif self.status in ['finished', 'stopped']:
            if self.current_file:
                msg += f"📄 File: {self.current_file}\n"
            msg += f"📈 Progress: {self.progress}%\n"
        
        return msg


class BambuPrinterMonitor:
    """Монітор принтерів Bambu Lab"""
    
    def __init__(self, debug_port: int = 9222, update_interval: int = 10,
                 exe_path: str = BAMBU_EXE_PATH, auto_launch: bool = True,
                 debug_logging: bool = False, debug_dump_dir: str = "debug_dumps"):
        """
        Args:
            debug_port: Порт Chrome DevTools
            update_interval: Інтервал оновлення у секундах
            exe_path: Шлях до Bambu Farm Manager Client.exe
            auto_launch: Автоматично запускати додаток якщо він не запущений
            debug_logging: Детальне логування (зокрема діагностика зникнення принтерів)
            debug_dump_dir: Куди зберігати сирий HTML дашборда при аномаліях
        """
        self.debug_port = debug_port
        self.update_interval = update_interval
        self.exe_path = exe_path
        self.auto_launch = auto_launch
        self.debug_logging = debug_logging
        self.debug_dump_dir = debug_dump_dir
        self.printers: Dict[str, PrinterStatus] = {}
        # name -> (candidate_status, скільки разів поспіль зчитано) — антидребезг статусу
        self._pending: Dict[str, tuple] = {}
        # скільки однакових зчитувань поспіль потрібно, щоб прийняти зміну статусу
        self.status_confirm_polls = 2
        # --- діагностика зникнення принтерів ---
        self._last_seen_names: set = set()           # назви, видимі у попередньому scrape
        self._last_card_count: Optional[int] = None  # скільки карток було минулого циклу
        self._last_parse_stats: dict = {}            # статистика останнього парсингу
        self._cycle_seq = 0                          # лічильник циклів оновлення
        self._debug_handler = None                   # окремий file-handler для debug
        self.running = False
        self.ws_connection: Optional[websocket.WebSocket] = None
        self.monitor_thread: Optional[threading.Thread] = None
        self.app_process: Optional[subprocess.Popen] = None
        
        # Callback функції для подій
        self.on_printer_status_change: Optional[Callable[[str, PrinterStatus, PrinterStatus], None]] = None
        self.on_print_complete: Optional[Callable[[str, PrinterStatus], None]] = None
        self.on_printer_online: Optional[Callable[[str, PrinterStatus], None]] = None
        self.on_printer_offline: Optional[Callable[[str, PrinterStatus], None]] = None
        self.on_update_complete: Optional[Callable[[], None]] = None  # Викликається після кожного оновлення

        # Налаштувати детальне логування, якщо увімкнено через конфіг
        self._configure_debug_logging()

    def is_app_running(self) -> bool:
        """Перевірка чи доступний Bambu Farm Manager на debug-порту"""
        try:
            resp = requests.get(f"http://127.0.0.1:{self.debug_port}/json", timeout=2)
            return resp.status_code == 200
        except Exception:
            return False

    def _process_name(self) -> str:
        """Ім'я виконуваного файлу клієнта (для tasklist/taskkill)"""
        return os.path.basename(self.exe_path)

    def _is_process_running(self) -> bool:
        """Чи запущений процес Bambu Farm Manager Client (будь-який інстанс)"""
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {self._process_name()}", "/NH"],
                capture_output=True, text=True, timeout=10
            )
            return self._process_name().lower() in result.stdout.lower()
        except Exception as e:
            logger.debug(f"tasklist не вдався: {e}")
            return False

    def _terminate_running_app(self) -> bool:
        """Закрити запущений Bambu Farm Manager Client: спершу м'яко, потім примусово"""
        name = self._process_name()

        logger.info("Закриваю запущений Bambu Farm Manager Client...")
        try:
            # без /F — надсилає WM_CLOSE вікнам процесу (звичайне закриття), /T — разом із дочірніми
            subprocess.run(["taskkill", "/IM", name, "/T"],
                           capture_output=True, text=True, timeout=10)
        except Exception as e:
            logger.debug(f"taskkill (м'яко) не вдався: {e}")

        for _ in range(5):
            time.sleep(1)
            if not self._is_process_running():
                logger.info("✓ Клієнт закрито")
                return True

        logger.warning("Клієнт не закрився за 5с — примусово (taskkill /F)")
        try:
            subprocess.run(["taskkill", "/F", "/IM", name, "/T"],
                           capture_output=True, text=True, timeout=10)
        except Exception as e:
            logger.debug(f"taskkill (примусово) не вдався: {e}")

        for _ in range(5):
            time.sleep(1)
            if not self._is_process_running():
                logger.info("✓ Клієнт закрито (примусово)")
                return True

        logger.error("❌ Не вдалося закрити Bambu Farm Manager Client")
        return False

    def launch_app(self) -> bool:
        """Запуск Bambu Farm Manager Client"""
        if not os.path.exists(self.exe_path):
            logger.error(f"❌ Файл не знайдено: {self.exe_path}")
            return False
        
        try:
            logger.info("🚀 Запускаємо Bambu Farm Manager Client...")
            self.app_process = subprocess.Popen(
                [self.exe_path, f"--remote-debugging-port={self.debug_port}", "--remote-allow-origins=*"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            
            # Чекаємо поки додаток запуститься
            for i in range(30):  # Максимум 30 секунд
                time.sleep(1)
                if self.is_app_running():
                    logger.info("✓ Додаток успішно запущено")
                    return True
                if i % 5 == 0:
                    logger.info(f"   Очікування запуску... ({i}s)")
            
            logger.error("❌ Додаток не запустився протягом 30 секунд")
            return False
            
        except Exception as e:
            logger.error(f"❌ Помилка запуску додатку: {e}")
            return False
    
    def get_dashboard_page(self) -> Optional[dict]:
        """Отримання сторінки Dashboard (як в export_dashboard.py)"""
        try:
            resp = requests.get(f"http://127.0.0.1:{self.debug_port}/json", timeout=10)
            pages = resp.json()
            
            logger.info(f"Знайдено {len(pages)} вкладок:")
            for p in pages:
                title = p.get('title', 'No title')
                url = p.get('url', '')[:70]
                logger.info(f"  • {title} | {url}")
            
            # Картки принтерів є ЛИШЕ на роуті #/monitor. Зазвичай це окреме
            # фонове dashboard-вікно, а головне вікно лишається для користувача.
            # Тому ПЕРШИМ ділом чіпляємось саме до #/monitor, не чіпаючи головне.
            def _usable(p):
                return (p.get("type") == "page" and "webSocketDebuggerUrl" in p
                        and not p.get("url", "").startswith("devtools://"))

            for page in pages:
                if _usable(page) and "#/monitor" in page.get("url", "").lower():
                    logger.info(f"✓ Dashboard-вікно (#/monitor): {page.get('title')}")
                    return page

            # Фолбек: єдине доступне вікно (його перемкне на #/monitor
            # connect_websocket). Якщо тримаєш dashboard окремим вікном — сюди не дійде.
            for page in pages:
                if _usable(page):
                    logger.info(f"✓ Fallback page (перемкну на #/monitor): "
                                f"{page.get('title')} | {page.get('url', '')[:50]}")
                    return page

            return pages[0] if pages else None
        except Exception as e:
            logger.error(f"Помилка отримання сторінки: {e}")
            return None
    
    def wait_for_page_load(self) -> bool:
        """Очікування завантаження сторінки (як в export_dashboard.py)"""
        try:
            # Чекаємо завантаження контенту (як в export_dashboard)
            logger.info("Чекаємо завантаження контенту...")
            time.sleep(5)
            
            # Скролимо сторінку для lazy-loaded контенту
            try:
                self._send_command(100, "Runtime.evaluate", {
                    "expression": "window.scrollTo(0, document.body.scrollHeight); setTimeout(() => window.scrollTo(0, 0), 500);",
                    "awaitPromise": False
                })
                time.sleep(2)
            except:
                pass
            
            logger.info("✓ Контент завантажено")
            return True
        except Exception as e:
            logger.error(f"Помилка очікування: {e}")
            return False
    
    def connect_websocket(self) -> bool:
        """Підключення до WebSocket"""
        try:
            page = self.get_dashboard_page()
            if not page or "webSocketDebuggerUrl" not in page:
                logger.error("Не вдалося знайти Dashboard сторінку")
                return False
            
            logger.info(f"Підключення до: {page.get('title', 'Unknown')} | {page.get('url', '')[:70]}")
            ws_url = page["webSocketDebuggerUrl"]
            self.ws_connection = websocket.create_connection(ws_url, timeout=15)
            
            # Увімкнення необхідних доменів
            self._send_command(1, "DOM.enable")
            self._send_command(2, "Page.enable")
            self._send_command(3, "Runtime.enable")
            # Network: щоб читати JSON (devices2), який клієнт сам тягне з ферми —
            # це точніше і надійніше за парсинг DOM.
            try:
                self._send_command(7, "Network.enable")
            except Exception as e:
                logger.debug(f"Network.enable не вдався: {e}")

            logger.info("✓ WebSocket підключено")
            
            # Очікування завантаження сторінки (як в export_dashboard.py)
            if not self.wait_for_page_load():
                logger.error("❌ Не вдалось завантажити сторінку")
                return False

            # Картки monitor_printer є ЛИШЕ на роуті #/monitor (вид Dashboard).
            # На інших вкладках (Printers/Tasks/Files/...) карток нема -> scrape 0.
            self._ensure_dashboard()
            time.sleep(2)

            return True

        except Exception as e:
            logger.error(f"Помилка підключення WebSocket: {e}")
            return False

    def _ensure_dashboard(self):
        """Тримати SPA на роуті #/monitor — тільки там рендеряться картки принтерів."""
        try:
            self._send_command(4, "Runtime.evaluate", {
                "expression": "if(location.hash!=='#/monitor'){location.hash='#/monitor';}",
                "awaitPromise": False,
            })
        except Exception as e:
            logger.debug(f"Не вдалось перемкнути на #/monitor: {e}")

    def _send_command(self, cmd_id: int, method: str, params: Optional[dict] = None) -> dict:
        """Відправка команди через WebSocket"""
        if not self.ws_connection:
            raise Exception("WebSocket не підключений")
        
        payload = {"id": cmd_id, "method": method}
        if params:
            payload["params"] = params
        
        self.ws_connection.send(json.dumps(payload))
        
        # Отримання відповіді
        while True:
            response = json.loads(self.ws_connection.recv())
            if "id" in response and response["id"] == cmd_id:
                return response
            if "method" in response:
                continue
        
        return response
    
    # ========================= JSON API (devices2) =========================
    def _fetch_devices_json(self, timeout: float = 8.0) -> Optional[list]:
        """Дочекатись наступної відповіді /devices2, яку клієнт сам тягне з ферми,
        і повернути список девайсів. Читаємо ws напряму — безпечно, бо цикл монітора
        однопотоковий. Жодних токенів і зовнішніх викликів: лише тіло відповіді, яке
        клієнт уже отримав."""
        ws = self.ws_connection
        if not ws:
            return None
        deadline = time.monotonic() + timeout
        req_id = None
        try:
            ws.settimeout(1.0)
            # 1) чекаємо свіжу відповідь devices2
            while time.monotonic() < deadline and req_id is None:
                try:
                    msg = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                if msg.get("method") == "Network.responseReceived":
                    url = msg.get("params", {}).get("response", {}).get("url", "")
                    if "devices2" in url:
                        req_id = msg["params"]["requestId"]
            if req_id is None:
                return None
            # 2) дочекатись поки тіло догрузиться
            while time.monotonic() < deadline:
                try:
                    msg = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    break
                if (msg.get("method") == "Network.loadingFinished"
                        and msg.get("params", {}).get("requestId") == req_id):
                    break
        finally:
            try:
                ws.settimeout(15)
            except Exception:
                pass
        # 3) забираємо тіло
        try:
            resp = self._send_command(120, "Network.getResponseBody", {"requestId": req_id})
        except Exception as e:
            logger.debug(f"getResponseBody не вдався: {e}")
            return None
        raw = resp.get("result", {}).get("body")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        devices = data.get("devices")
        return devices if isinstance(devices, list) else None

    @staticmethod
    def _model_from_name(name: str) -> str:
        m = re.search(r'\(([^)]{1,20})\)\s*$', name or "")
        return m.group(1).strip() if m else ""

    @staticmethod
    def _fmt_remaining(minutes) -> Optional[str]:
        try:
            m = int(minutes)
        except (TypeError, ValueError):
            return None
        if m <= 0:
            return None
        h, mi = divmod(m, 60)
        return f"-{h}h{mi}m" if h else f"-{mi}m"

    @staticmethod
    def _fmt_temp(actual, target) -> str:
        def _n(v):
            try:
                return str(round(float(v)))
            except (TypeError, ValueError):
                return "0"
        return f"{_n(actual)}/{_n(target)}°C"

    def _hms_message(self, rs: dict) -> str:
        """Причина паузи/помилки з полів hms[] / print_error.

        hms entries виглядають як {attr, code}; канонічний HMS-ідентифікатор —
        HEX16 (XXXX_XXXX_XXXX_XXXX). Точний локалізований текст резолвиться через
        i18n клієнта; поки що віддаємо код, а сирі hms логуємо для зведення мапи.
        """
        hms = rs.get("hms") or []
        if hms:
            if self.debug_logging:
                logger.debug("HMS raw: %s", hms)
            codes = []
            for h in hms:
                try:
                    attr = int(h.get("attr", 0))
                    code = int(h.get("code", 0))
                    codes.append("%04X_%04X_%04X_%04X" % (
                        attr >> 16 & 0xFFFF, attr & 0xFFFF, code >> 16 & 0xFFFF, code & 0xFFFF))
                except Exception:
                    continue
            if codes:
                return "HMS " + ", ".join(codes)
        try:
            err = int(rs.get("print_error", 0) or 0)
        except (TypeError, ValueError):
            err = 0
        if err:
            return "Print error 0x%08X" % (err & 0xFFFFFFFF)
        return ""

    def _parse_devices_json(self, devices: list) -> List[PrinterStatus]:
        """Перетворити devices2 JSON у список PrinterStatus (точне джерело даних)."""
        out = []
        parsed_ok = 0
        for d in devices:
            try:
                rs = d.get("report_status", {}) or {}
                name = (d.get("name") or d.get("dev_name") or d.get("dev_id") or "").strip()
                if not name:
                    continue
                online = bool(d.get("online", True))
                gstate = str(rs.get("gcode_state", "") or "").upper()

                try:
                    progress = int(rs.get("mc_percent") or 0)
                except (TypeError, ValueError):
                    progress = 0
                remaining_time = self._fmt_remaining(rs.get("mc_remaining_time"))

                status = _GCODE_STATE.get(gstate)
                if status is None:
                    status = "printing" if (progress > 0 and remaining_time) else "idle"
                if not online:
                    status = "offline"
                if status == "finished":
                    progress = 100

                model = self._model_from_name(name) or _MODEL_CODES.get(
                    str(d.get("dev_model", "")), str(d.get("dev_model", "") or ""))
                try:
                    spd = int(rs.get("spd_lvl") or 2)
                except (TypeError, ValueError):
                    spd = 2
                speed = _SPEED_LVL.get(spd, "Standard")

                current_file = (rs.get("subtask_name") or rs.get("gcode_file") or "").strip() or None
                try:
                    layer = int(rs.get("layer_num") or 0)
                    total_layers = int(rs.get("total_layer_num") or 0)
                except (TypeError, ValueError):
                    layer = total_layers = 0

                out.append(PrinterStatus(
                    name=name,
                    model=model,
                    status=status,
                    progress=progress,
                    current_file=current_file,
                    remaining_time=remaining_time,
                    nozzle_temp=self._fmt_temp(rs.get("nozzle_temper"), rs.get("nozzle_target_temper")),
                    bed_temp=self._fmt_temp(rs.get("bed_temper"), rs.get("bed_target_temper")),
                    speed=speed,
                    online=online,
                    last_update=datetime.now(),
                    message=self._hms_message(rs),
                    serial=str(d.get("dev_id", "") or ""),
                    layer=layer,
                    total_layers=total_layers,
                ))
                parsed_ok += 1
            except Exception as e:
                logger.debug(f"devices2: помилка девайса: {e}")
                continue
        self._last_parse_stats = {
            'cards_found': len(devices), 'parsed_ok': parsed_ok,
            'skipped_no_name': len(devices) - parsed_ok, 'skipped_no_status': 0,
            'parse_errors': 0, 'skipped_names': [],
        }
        return out

    def parse_printers_from_html(self, html: str) -> List[PrinterStatus]:
        """Парсинг принтерів з HTML"""
        soup = BeautifulSoup(html, 'html.parser')
        printers = []

        # Знаходимо всі картки принтерів
        printer_cards = soup.find_all('div', class_='monitor_printer')

        # Статистика парсингу — основа діагностики зникнення принтерів
        stats = {
            'cards_found': len(printer_cards),
            'parsed_ok': 0,
            'skipped_no_name': 0,
            'skipped_no_status': 0,
            'parse_errors': 0,
            'skipped_names': [],
        }

        if not printer_cards:
            logger.warning(f"Не знайдено карток принтерів. HTML довжина: {len(html)} символів")
            # Debug: виводимо які класи є
            all_divs = soup.find_all('div', class_=True)
            classes = set()
            for div in all_divs[:20]:  # Перші 20 div'ів
                classes.update(div.get('class', []))
            if classes:
                logger.debug(f"Знайдені класи: {', '.join(list(classes)[:10])}")
        
        for card in printer_cards:
            try:
                # Назва принтера
                name_elem = card.find('div', class_='monitor_printer-name')
                name = name_elem.get_text(strip=True) if name_elem else ""
                if not name:
                    stats['skipped_no_name'] += 1
                    logger.debug("Картка принтера без назви — пропускаю (можливо, ще рендериться)")
                    continue

                # Файл що друкується
                file_elem = card.find('div', class_='monitor_printing_file')
                current_file = file_elem.get_text(strip=True) if file_elem else None

                # Статус з прогресом
                status_elem = card.find('div', class_='monitor_printer-status')
                status_text = status_elem.get_text(strip=True) if status_elem else ""
                if not status_text:
                    # Картка є, але блок статусу ще не намальований — не вигадуємо offline
                    stats['skipped_no_status'] += 1
                    stats['skipped_names'].append(name)
                    logger.debug(f"'{name}': картка без статусу — пропускаю цей цикл")
                    continue
                
                # Парсинг прогресу та часу: "7% -7h6m" або "Paused 45%"
                progress = 0
                remaining_time = None
                if '%' in status_text:
                    parts = status_text.split()
                    # Шукаємо частину з %
                    for part in parts:
                        if '%' in part:
                            try:
                                progress = int(part.replace('%', ''))
                                break
                            except ValueError:
                                # Якщо не вдалося конвертувати - пропускаємо
                                pass
                    # Шукаємо час (починається з '-')
                    for part in parts:
                        if part.startswith('-') and ('h' in part or 'm' in part):
                            remaining_time = part
                            break
                
                # Температури - шукаємо всі тексти з °C
                temp_texts = card.find_all('span')
                nozzle_temp = "0/0°C"
                bed_temp = "0/0°C"
                speed = "Standard"
                
                temp_values = []
                for span in temp_texts:
                    text = span.get_text(strip=True)
                    if '°C' in text and '/' in text:
                        temp_values.append(text)
                    elif text in ['Silent', 'Standard', 'Sport', 'Ludicrous']:
                        speed = text
                
                # Перша температура - nozzle, друга - bed
                if len(temp_values) >= 1:
                    nozzle_temp = temp_values[0]
                if len(temp_values) >= 2:
                    bed_temp = temp_values[1]
                
                # Визначення статусу та онлайн. Дашборд для активного друку дає
                # просто "10% -6h53m" (без слова printing), тож прогрес і час що
                # лишився це теж ознака друку.
                status_lower = status_text.lower()
                online = 'offline' not in status_lower and 'disconnect' not in status_lower

                # A paused print often keeps showing its progress %, so the word
                # "pause" may live in an icon/badge inside the status block or in a
                # modifier class on the card rather than in the status text. Look in
                # the status element subtree and the card's own classes too (both are
                # state markup, not the always present pause control button).
                status_html = str(status_elem).lower() if status_elem else ""
                card_classes = " ".join(card.get('class', []) or []).lower()
                is_paused = ('paus' in status_lower or 'paus' in status_html
                             or 'paus' in card_classes)

                if 'finish' in status_lower or 'complete' in status_lower:
                    status = 'finished'
                elif is_paused:
                    status = 'paused'
                elif ('stop' in status_lower or 'cancel' in status_lower
                        or 'error' in status_lower or 'fail' in status_lower):
                    status = 'stopped'
                elif ('print' in status_lower or 'heat' in status_lower
                        or 'prepar' in status_lower or 'running' in status_lower
                        or progress > 0 or remaining_time):
                    status = 'printing'
                elif not online:
                    status = 'offline'
                else:
                    status = 'idle'

                # A finished print is 100% by definition; the status line often shows
                # "Finished" without a percent, which would otherwise read as 0%.
                if status == 'finished':
                    progress = 100

                # Pause/error reason (HMS). On the card it is the red warning icon in the
                # corner whose hover tooltip holds the text, so it lives in a title /
                # aria-label attribute or an SVG <title>, not in the visible text. Check
                # those first; fall back to a descriptive sentence in the visible text.
                def _good_msg(v):
                    v = (v or "").strip()
                    if len(v) >= 18 and v.count(' ') >= 3 and any(c.isalpha() for c in v):
                        return v
                    return ""

                message = ""
                for _t in card.find_all('title'):  # SVG/HTML <title> (icon tooltip)
                    message = _good_msg(_t.get_text())
                    if message:
                        break
                if not message:
                    for _el in card.find_all(True):
                        for _attr in ('title', 'aria-label', 'data-tooltip',
                                      'data-original-title', 'data-tip', 'alt'):
                            message = _good_msg(_el.get(_attr))
                            if message:
                                break
                        if message:
                            break
                if not message:
                    _known = {name, current_file or "", speed, nozzle_temp, bed_temp, status_text}
                    for _s in card.stripped_strings:
                        _s = _s.strip()
                        if not _s or _s in _known:
                            continue
                        if '%' in _s or _s.endswith('°C') or re.match(r'^-?\d+\s*[hm]', _s):
                            continue
                        if _s in ('Silent', 'Standard', 'Sport', 'Ludicrous'):
                            continue
                        if len(_s) >= 18 and _s.count(' ') >= 3 and any(c.isalpha() for c in _s):
                            message = _s
                            break

                # Модель: на дашборді вона у дужках наприкінці назви, напр. "2. (A1)".
                # Беремо вміст останніх дужок — покриває будь-яку модель
                # (A1, A1 mini, X1, X1C, X1E, P1P, P1S, H2D тощо).
                model = "A1"
                mm = re.search(r'\(([^)]{1,20})\)\s*$', name)
                if mm:
                    model = mm.group(1).strip()
                
                printer = PrinterStatus(
                    name=name,
                    model=model,
                    status=status,
                    progress=progress,
                    current_file=current_file,
                    remaining_time=remaining_time,
                    nozzle_temp=nozzle_temp,
                    bed_temp=bed_temp,
                    speed=speed,
                    online=online,
                    last_update=datetime.now(),
                    message=message,
                )
                
                printers.append(printer)
                stats['parsed_ok'] += 1
                if self.debug_logging:
                    logger.debug(
                        f"   ✓ картка: '{name}' status={status} progress={progress} "
                        f"online={online} file={current_file!r}"
                    )

            except Exception as e:
                stats['parse_errors'] += 1
                logger.error(f"Помилка парсингу принтера: {e}")
                continue

        self._last_parse_stats = stats
        return printers
    
    def update_printers(self) -> bool:
        """Оновлення статусу всіх принтерів.

        Основне джерело — JSON (devices2), який клієнт сам тягне з ферми: точні
        статуси, прогрес, температури, причини пауз. Якщо JSON недоступний (інша
        версія клієнта/нема трафіку) — фолбек на парсинг DOM #/monitor.
        """
        try:
            devices = None
            try:
                devices = self._fetch_devices_json()
            except Exception as e:
                logger.debug(f"devices2 JSON недоступний, фолбек на DOM: {e}")

            if devices is not None:
                new_printers = self._parse_devices_json(devices)
                logger.debug("✓ дані з devices2 JSON: %d принтерів", len(new_printers))
                return self._finish_update(new_printers, None)

            # ---- Фолбек: парсинг DOM (#/monitor) ----
            html = None

            # Спосіб 1: DOM.getDocument + DOM.getOuterHTML
            try:
                doc = self._send_command(5, "DOM.getDocument", {"depth": -1, "pierce": True})
                if "result" in doc and "root" in doc["result"]:
                    root_node_id = doc["result"]["root"]["nodeId"]
                    html_resp = self._send_command(6, "DOM.getOuterHTML", {"nodeId": root_node_id})
                    if "result" in html_resp and "outerHTML" in html_resp["result"]:
                        html = html_resp["result"]["outerHTML"]
                        logger.debug("✓ HTML отримано через DOM API")
            except Exception as e:
                logger.debug(f"DOM API не вдався: {e}")
            
            # Спосіб 2: JavaScript (якщо DOM API не спрацював)
            if not html:
                logger.debug("Використовуємо JavaScript для отримання HTML...")
                html_resp = self._send_command(110, "Runtime.evaluate", {
                    "expression": "document.documentElement.outerHTML",
                    "returnByValue": True
                })
                
                if "result" in html_resp:
                    result = html_resp["result"]
                    if "value" in result:
                        html = result["value"]
                        logger.debug("✓ HTML отримано через JavaScript (value)")
                    elif "objectId" in result:
                        obj_resp = self._send_command(111, "Runtime.callFunctionOn", {
                            "objectId": result["objectId"],
                            "functionDeclaration": "function() { return this; }",
                            "returnByValue": True
                        })
                        if "result" in obj_resp and "value" in obj_resp["result"]:
                            html = obj_resp["result"]["value"]
                            logger.debug("✓ HTML отримано через objectId")
                    elif "description" in result:
                        html = result["description"]
                        logger.debug("✓ HTML отримано через description")
            
            # Спосіб 3: innerHTML (остання спроба)
            if not html:
                logger.debug("Остання спроба через innerHTML...")
                html_resp2 = self._send_command(112, "Runtime.evaluate", {
                    "expression": "'<html>' + document.documentElement.innerHTML + '</html>'",
                    "returnByValue": True
                })
                if "result" in html_resp2 and "value" in html_resp2["result"]:
                    html = html_resp2["result"]["value"]
                    logger.debug("✓ HTML отримано через innerHTML")
            
            if not html:
                logger.error("❌ Не вдалося отримати HTML жодним способом")
                return False

            new_printers = self.parse_printers_from_html(html)
            return self._finish_update(new_printers, html)

        except Exception as e:
            logger.error(f"Помилка оновлення принтерів: {e}")
            return False

    def _finish_update(self, new_printers: List[PrinterStatus], html: Optional[str]) -> bool:
        """Спільний хвіст оновлення для обох джерел (JSON і DOM): діагностика
        зникнень, антидребезг статусів, callback."""
        # Принтерів нема взагалі -> найімовірніше клієнт пішов з роуту дашборда
        # (#/monitor). Повертаємо його, щоб наступний цикл був уже з даними.
        if not new_printers and self._last_parse_stats.get('cards_found', 0) == 0:
            self._ensure_dashboard()

        # Діагностика зникнення принтерів — ніколи не має ламати моніторинг.
        try:
            self._diagnose_cycle(new_printers, html)
        except Exception as e:
            logger.error(f"Помилка діагностики циклу: {e}")

        # Зміни статусу приймаємо лише після підтвердження кілька зчитувань поспіль,
        # щоб одноразові блимання не давали хибних сповіщень.
        for new_printer in new_printers:
            self._apply_printer_update(new_printer)

        logger.info(f"✓ Оновлено статус {len(new_printers)} принтерів")

        if self.on_update_complete:
            self.on_update_complete()

        return True

    def _apply_printer_update(self, new_printer: PrinterStatus):
        """Прийняти оновлення одного принтера з антидребезгом статусу.

        Зміну статусу приймаємо (і шлемо сповіщення) лише після того, як вона
        підтвердилась status_confirm_polls зчитувань поспіль. Доти "м'які" поля
        (прогрес, температури) оновлюються, а статус лишається попередній.
        """
        name = new_printer.name
        old = self.printers.get(name)

        # Перша поява принтера — приймаємо одразу, без сповіщень
        if old is None:
            self.printers[name] = new_printer
            self._pending.pop(name, None)
            return

        def _check_print_complete(prev: PrinterStatus, cur: PrinterStatus):
            # Завершення друку — лише по переходу прогресу в 100 і коли статус
            # не став 'finished' (щоб не дублювати сповіщення про зміну статусу)
            if (0 < prev.progress < 100 and cur.progress == 100
                    and cur.status.lower() != 'finished'
                    and self.on_print_complete):
                self.on_print_complete(name, cur)

        # Статус не змінився — оновлюємо дані, скидаємо лічильник підтверджень
        if old.status == new_printer.status:
            _check_print_complete(old, new_printer)
            self.printers[name] = new_printer
            self._pending.pop(name, None)
            return

        # Статус відрізняється — кандидат на зміну, рахуємо підтвердження
        pending = self._pending.get(name)
        if pending and pending[0] == new_printer.status:
            count = pending[1] + 1
        else:
            count = 1
        self._pending[name] = (new_printer.status, count)

        if count < self.status_confirm_polls:
            # Ще не підтверджено — оновлюємо лише "м'які" поля, статус лишаємо старий
            self.printers[name] = replace(
                new_printer, status=old.status, online=old.online
            )
            logger.debug(
                f"{name}: '{old.status}' → '{new_printer.status}'? "
                f"очікую підтвердження ({count}/{self.status_confirm_polls})"
            )
            return

        # Підтверджено N разів поспіль — приймаємо зміну і шлемо сповіщення
        self._pending.pop(name, None)
        logger.info(f"{name}: статус '{old.status}' → '{new_printer.status}'")

        if self.on_printer_status_change:
            self.on_printer_status_change(name, old, new_printer)
        _check_print_complete(old, new_printer)
        if not old.online and new_printer.online:
            if self.on_printer_online:
                self.on_printer_online(name, new_printer)
        elif old.online and not new_printer.online:
            if self.on_printer_offline:
                self.on_printer_offline(name, new_printer)

        self.printers[name] = new_printer

    # === ДІАГНОСТИКА ЗНИКНЕННЯ ПРИНТЕРІВ ===

    def _configure_debug_logging(self):
        """Увімкнути/вимкнути детальне логування монітора (окремий debug-лог)."""
        if self.debug_logging:
            logger.setLevel(logging.DEBUG)
            if self._debug_handler is None:
                try:
                    h = logging.FileHandler('printer_monitor.debug.log', encoding='utf-8')
                    h.setLevel(logging.DEBUG)
                    h.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
                    logger.addHandler(h)
                    self._debug_handler = h
                    logger.info("🔧 Окремий debug-лог: printer_monitor.debug.log")
                except Exception as e:
                    logger.error(f"Не вдалося створити debug-лог: {e}")
        else:
            logger.setLevel(logging.INFO)

    def set_debug_logging(self, enabled: bool):
        """Перемкнути детальне логування під час роботи (виклик із бота)."""
        self.debug_logging = enabled
        self._configure_debug_logging()
        logger.info(
            f"🔧 Детальне логування монітора: {'УВІМКНЕНО' if enabled else 'вимкнено'}"
        )

    def _dump_html(self, html: str, reason: str):
        """Зберегти сирий HTML дашборда для подальшого аналізу аномалії."""
        try:
            d = Path(self.debug_dump_dir)
            d.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            path = d / f"dashboard_{ts}_{reason}.html"
            path.write_text(html or "", encoding='utf-8')
            logger.warning(f"💾 HTML дашборда збережено: {path} ({len(html or '')} байт)")
        except Exception as e:
            logger.error(f"Не вдалося зберегти HTML-дамп: {e}")

    def _diagnose_cycle(self, new_printers: List[PrinterStatus], html: str):
        """Порівняти поточний scrape із попереднім і зафіксувати зникнення принтерів.

        Дешево й працює завжди. Якщо принтер був видимий минулого циклу, а зараз
        його нема у scrape — це і є "зникнення". При debug_logging додатково
        зберігається сирий HTML для forensics.
        """
        self._cycle_seq += 1
        new_names = {p.name for p in new_printers}
        known_names = set(self.printers.keys())
        stats = self._last_parse_stats or {}
        card_count = stats.get('cards_found', len(new_printers))

        vanished = self._last_seen_names - new_names   # було минулого циклу, зникло зараз
        appeared = new_names - self._last_seen_names    # нові у цьому циклі

        logger.info(
            "🔎 цикл #%d: карток=%d, розпарсено=%d, scrape=%d, у пам'яті=%d",
            self._cycle_seq, card_count, stats.get('parsed_ok', len(new_printers)),
            len(new_names), len(known_names),
        )

        skipped_total = (stats.get('skipped_no_name', 0)
                         + stats.get('skipped_no_status', 0)
                         + stats.get('parse_errors', 0))
        if skipped_total:
            logger.warning(
                "   ⚠️ пропущено карток: без_назви=%d, без_статусу=%d (%s), помилок=%d",
                stats.get('skipped_no_name', 0),
                stats.get('skipped_no_status', 0),
                ", ".join(stats.get('skipped_names', [])) or "-",
                stats.get('parse_errors', 0),
            )

        anomaly = False

        if vanished:
            anomaly = True
            logger.warning(
                "⚠️ ПРИНТЕРИ ЗНИКЛИ з дашборда (%d): %s | минулий цикл=%d, зараз=%d",
                len(vanished), ", ".join(sorted(vanished)),
                len(self._last_seen_names), len(new_names),
            )

        if appeared and self._last_seen_names:
            logger.info("➕ зʼявились (%d): %s", len(appeared), ", ".join(sorted(appeared)))

        if self._last_card_count is not None and card_count < self._last_card_count:
            anomaly = True
            logger.warning("⚠️ кількість карток впала: %d → %d",
                           self._last_card_count, card_count)

        if card_count == 0:
            anomaly = True
            logger.warning("⚠️ scrape повернув 0 карток (html=%d байт)", len(html or ""))

        # принтери, що лишилися в пам'яті, але зникли зі scrape (показуються stale)
        stale = known_names - new_names
        if stale and self.debug_logging:
            logger.debug("   у пам'яті, але не у scrape (%d): %s",
                         len(stale), ", ".join(sorted(stale)))

        if anomaly and self.debug_logging:
            self._dump_html(html, reason=f"cycle{self._cycle_seq}_cards{card_count}")

        self._last_seen_names = new_names
        self._last_card_count = card_count

    def get_summary(self) -> dict:
        """Отримання загальної статистики"""
        # знімок — щоб монітор-потік не змінив dict під час підрахунку
        printers = list(self.printers.values())
        online = sum(1 for p in printers if p.online)
        printing = sum(1 for p in printers if p.status.lower() == 'printing')
        idle = sum(1 for p in printers if p.status.lower() == 'idle' and p.online)
        finished = sum(1 for p in printers if p.status.lower() == 'finished')
        paused = sum(1 for p in printers if p.status.lower() == 'paused')

        logger.debug(
            "📊 total=%d online=%d printing=%d paused=%d finished=%d idle=%d offline=%d",
            len(printers), online, printing, paused, finished, idle, len(printers) - online,
        )

        return {
            'total': len(printers),
            'online': online,
            'printing': printing,
            'idle': idle,
            'finished': finished,
            'paused': paused,
            'offline': len(printers) - online,
        }
    
    def _reconnect(self) -> bool:
        """Перепідключити CDP websocket після обриву чи перезавантаження дашборда.

        Закриває старе зʼєднання, за потреби (і якщо auto_launch) перезапускає
        клієнт, і піднімає websocket наново. Повертає True при успіху.
        """
        try:
            if self.ws_connection:
                try:
                    self.ws_connection.close()
                except Exception:
                    pass
                self.ws_connection = None

            if not self.is_app_running():
                if self.auto_launch:
                    logger.info("Клієнт недоступний на debug-порту — перезапускаю")
                    if not self.launch_app():
                        return False
                else:
                    logger.warning("Клієнт недоступний на debug-порту і auto_launch вимкнено")
                    return False

            if self.connect_websocket():
                logger.info("✓ CDP перепідключено")
                return True
            return False
        except Exception as e:
            logger.error(f"Перепідключення не вдалось: {e}")
            return False

    def _monitor_loop(self):
        """Основний цикл моніторингу"""
        logger.info("🚀 Моніторинг запущено")

        failures = 0
        while self.running:
            try:
                ok = self.update_printers()
                if ok:
                    failures = 0
                else:
                    failures += 1
                    if failures >= 2:
                        logger.warning("Кілька невдалих оновлень поспіль — перепідключаю CDP")
                        if self._reconnect():
                            failures = 0
                time.sleep(self.update_interval)
            except Exception as e:
                logger.error(f"Помилка у циклі моніторингу: {e}")
                failures += 1
                if failures >= 2 and self._reconnect():
                    failures = 0
                time.sleep(5)  # Чекаємо перед повторною спробою
    
    def start(self) -> bool:
        """Запуск моніторингу"""
        if self.running:
            logger.warning("Моніторинг вже запущений")
            return False
        
        # Перевіряємо чи доступний клієнт на debug-порту
        if self.is_app_running():
            logger.info("✓ Bambu Farm Manager вже запущений з debug-портом")
        else:
            # debug-порт не відповідає. Можливо, клієнт запущений у звичайному режимі —
            # тоді повторний запуск з --remote-debugging-port нічого не дасть (Electron
            # передасть керування першому інстансу), тому спершу закриваємо існуючий.
            if self.auto_launch:
                if self._is_process_running():
                    logger.info("Клієнт запущений без debug-порту — перезапускаю...")
                    self._terminate_running_app()
                    time.sleep(2)
                logger.info("Запуск Bambu Farm Manager Client з debug-портом...")
                if not self.launch_app():
                    return False
            else:
                logger.error(
                    "❌ Bambu Farm Manager не доступний на debug-порту. Закрийте клієнт "
                    "і ввімкніть auto_launch, або запустіть його вручну з --remote-debugging-port."
                )
                return False
        
        if not self.connect_websocket():
            return False
        
        self.running = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        
        logger.info("✓ Моніторинг успішно запущено")
        return True
    
    def stop(self, close_app: bool = False):
        """Зупинка моніторингу
        
        Args:
            close_app: Закрити Bambu Farm Manager Client після зупинки
        """
        logger.info("Зупинка моніторингу...")
        self.running = False
        
        if self.ws_connection:
            try:
                self.ws_connection.close()
            except:
                pass
        
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        
        if close_app and self.app_process:
            try:
                logger.info("Закриваємо Bambu Farm Manager Client...")
                self.app_process.terminate()
                self.app_process.wait(timeout=5)
                logger.info("✓ Додаток закрито")
            except:
                try:
                    self.app_process.kill()
                except:
                    pass
        
        logger.info("✓ Моніторинг зупинено")
    
    def get_printer(self, name: str) -> Optional[PrinterStatus]:
        """Отримання статусу конкретного принтера"""
        return self.printers.get(name)
    
    def get_all_printers(self) -> List[PrinterStatus]:
        """Отримання списку всіх принтерів"""
        return list(self.printers.values())


# === ПРИКЛАД ВИКОРИСТАННЯ ===

def example_callbacks():
    """Приклад callback функцій"""
    
    def on_status_change(name: str, old: PrinterStatus, new: PrinterStatus):
        logger.info(f"🔄 {name}: {old.status} → {new.status}")
    
    def on_print_complete(name: str, printer: PrinterStatus):
        logger.info(f"✅ {name}: Друк завершено! Файл: {printer.current_file}")
    
    def on_printer_online(name: str, printer: PrinterStatus):
        logger.info(f"🟢 {name}: Принтер онлайн")
    
    def on_printer_offline(name: str, printer: PrinterStatus):
        logger.warning(f"🔴 {name}: Принтер офлайн")
    
    return on_status_change, on_print_complete, on_printer_online, on_printer_offline


def main():
    """Основна функція для тестування"""
    monitor = BambuPrinterMonitor(update_interval=10)
    
    # Встановлення callback функцій
    callbacks = example_callbacks()
    monitor.on_printer_status_change = callbacks[0]
    monitor.on_print_complete = callbacks[1]
    monitor.on_printer_online = callbacks[2]
    monitor.on_printer_offline = callbacks[3]
    
    # Запуск моніторингу
    if not monitor.start():
        logger.error("❌ Не вдалося запустити моніторинг")
        return
    
    try:
        # Виведення статистики кожні 30 секунд
        while True:
            time.sleep(30)
            summary = monitor.get_summary()
            logger.info(f"📊 Статистика: {summary}")
            
            # Виведення детальної інформації про кожен принтер
            for printer in monitor.get_all_printers():
                logger.info(f"   {printer.name}: {printer.status} ({printer.progress}%)")
    
    except KeyboardInterrupt:
        logger.info("\n⏹️  Отримано сигнал зупинки...")
    finally:
        monitor.stop()


if __name__ == "__main__":
    main()
