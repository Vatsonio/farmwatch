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
                 exe_path: str = BAMBU_EXE_PATH, auto_launch: bool = True):
        """
        Args:
            debug_port: Порт Chrome DevTools
            update_interval: Інтервал оновлення у секундах
            exe_path: Шлях до Bambu Farm Manager Client.exe
            auto_launch: Автоматично запускати додаток якщо він не запущений
        """
        self.debug_port = debug_port
        self.update_interval = update_interval
        self.exe_path = exe_path
        self.auto_launch = auto_launch
        self.printers: Dict[str, PrinterStatus] = {}
        # name -> (candidate_status, скільки разів поспіль зчитано) — антидребезг статусу
        self._pending: Dict[str, tuple] = {}
        # скільки однакових зчитувань поспіль потрібно, щоб прийняти зміну статусу
        self.status_confirm_polls = 2
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
            
            # Логіка як в export_dashboard.py
            for page in pages:
                title = page.get("title", "").lower()
                url = page.get("url", "").lower()
                
                # Шукаємо Dashboard за ключовими словами
                if any(x in title for x in ["dashboard", "farm", "принтери", "bambu"]):
                    logger.info(f"✓ Знайдено Dashboard за назвою: {page['title']}")
                    return page
                if any(x in url for x in ["/dashboard", "/printers", "index.html", "/home", "#/monitor", "#/printers"]):
                    logger.info(f"✓ Знайдено Dashboard за URL: {url}")
                    return page
                
                # Перша page (не devtools)
                if "webSocketDebuggerUrl" in page and page.get("type") == "page":
                    if not any(p.get("url", "").startswith("devtools://") for p in pages):
                        logger.info(f"✓ Вибрано першу page: {page['title']}")
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
            
            logger.info("✓ WebSocket підключено")
            
            # Очікування завантаження сторінки (як в export_dashboard.py)
            if not self.wait_for_page_load():
                logger.error("❌ Не вдалось завантажити сторінку")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Помилка підключення WebSocket: {e}")
            return False
    
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
    
    def parse_printers_from_html(self, html: str) -> List[PrinterStatus]:
        """Парсинг принтерів з HTML"""
        soup = BeautifulSoup(html, 'html.parser')
        printers = []
        
        # Знаходимо всі картки принтерів
        printer_cards = soup.find_all('div', class_='monitor_printer')
        
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
                    elif text in ['Standard', 'Sport', 'Ludicrous']:
                        speed = text
                
                # Перша температура - nozzle, друга - bed
                if len(temp_values) >= 1:
                    nozzle_temp = temp_values[0]
                if len(temp_values) >= 2:
                    bed_temp = temp_values[1]
                
                # Визначення статусу та онлайн
                # Принтер онлайн якщо статус НЕ "offline"
                status_lower = status_text.lower()
                online = 'offline' not in status_lower
                
                # Статус друку
                if 'finished' in status_lower:
                    status = 'finished'
                elif 'paused' in status_lower or 'pause' in status_lower:
                    status = 'paused'
                elif 'stopped' in status_lower or 'stop' in status_lower:
                    status = 'stopped'
                elif progress > 0:
                    status = 'printing'
                elif online:
                    status = 'idle'
                else:
                    status = 'offline'
                
                # Модель (витягуємо з назви)
                model = "A1"  # За замовчуванням
                if '(A1)' in name or '(a1)' in name.lower():
                    model = "A1"
                elif '(X1)' in name or '(x1)' in name.lower():
                    model = "X1"
                elif '(P1)' in name or '(p1)' in name.lower():
                    model = "P1"
                
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
                    last_update=datetime.now()
                )
                
                printers.append(printer)
                
            except Exception as e:
                logger.error(f"Помилка парсингу принтера: {e}")
                continue
        
        return printers
    
    def update_printers(self) -> bool:
        """Оновлення статусу всіх принтерів"""
        try:
            # Отримуємо HTML як в export_dashboard.py - через DOM API
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

            # Порівнюємо зі старими даними; зміни статусу приймаємо лише після
            # підтвердження кілька зчитувань поспіль — щоб одноразові "блимання"
            # дашборда не давали хибних offline/online/finished сповіщень.
            for new_printer in new_printers:
                self._apply_printer_update(new_printer)

            logger.info(f"✓ Оновлено статус {len(new_printers)} принтерів")
            
            # Викликаємо callback після завершення оновлення
            if self.on_update_complete:
                self.on_update_complete()
            
            return True
            
        except Exception as e:
            logger.error(f"Помилка оновлення принтерів: {e}")
            return False

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
    
    def _monitor_loop(self):
        """Основний цикл моніторингу"""
        logger.info("🚀 Моніторинг запущено")
        
        while self.running:
            try:
                self.update_printers()
                time.sleep(self.update_interval)
            except Exception as e:
                logger.error(f"Помилка у циклі моніторингу: {e}")
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
