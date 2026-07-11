"""Serial output модуль: віддає стан ферми у ESP32-C3 матрицю 8x8 по COM порту.

farmwatch читає Bambu client через CDP (єдиний процес), а цей модуль лише
бере вже готовий список PrinterStatus і шле компактний текстовий кадр у ESP.
Так дисплей не конфліктує з рештою farmwatch.

Протокол (текст, по рядку):
    FW|<count>|<entry>,<entry>,...\\n
    entry = <letter><progress>,  progress = 0..100

Мапа станів -> літера: printing=p, idle=i, finished=d, stopped=e, paused=a, offline=o.
"""

import logging
import re
import threading
import time

logger = logging.getLogger(__name__)

# PrinterStatus.status -> літера протоколу
_STATUS_LETTER = {
    "printing": "p",
    "idle": "i",
    "finished": "d",
    "stopped": "e",
    "paused": "a",
    "offline": "o",
}
_DEFAULT_LETTER = "i"
_MAX_PRINTERS = 8
_NAME_SEP = "\x1f"          # роздільник значення/назви всередині запису
_NAME_MAXLEN = 16
_NON_ASCII = re.compile(r"[^\x20-\x7e]")  # усе поза друкованим ASCII (шрифт матриці лише латиниця)


def _ascii_name(s):
    """Назва принтера у безпечний для матриці ASCII рядок (без роздільників)."""
    s = _NON_ASCII.sub("", str(s or ""))
    s = s.replace(",", " ")           # кома розділяє записи
    s = " ".join(s.split())           # стиснути пробіли
    return s[:_NAME_MAXLEN].strip()

# USB VID для автопошуку плати: спершу Espressif (native USB), далі мости USB-UART
_ESPRESSIF_VIDS = (0x303A,)
_BRIDGE_VIDS = (0x10C4, 0x1A86, 0x0403)  # CP210x, CH340, FTDI


def autodetect_port(ports=None):
    """Знайти COM порт ESP по USB VID. Повертає назву порту або None.

    ports можна передати для тесту; інакше береться serial.tools.list_ports.
    """
    if ports is None:
        try:
            from serial.tools import list_ports
            ports = list(list_ports.comports())
        except Exception:
            return None
    for vids in (_ESPRESSIF_VIDS, _BRIDGE_VIDS):
        for p in ports:
            if getattr(p, "vid", None) in vids:
                return p.device
    return None


def _entry(printer, use_names=False):
    """Один принтер: <letter><progress>, за потреби + \\x1f<name>. Duck typing по PrinterStatus."""
    status = getattr(printer, "status", None) or "idle"
    if not getattr(printer, "online", True):
        status = "offline"
    letter = _STATUS_LETTER.get(status, _DEFAULT_LETTER)
    try:
        prog = int(getattr(printer, "progress", 0) or 0)
    except (TypeError, ValueError):
        prog = 0
    prog = max(0, min(100, prog))
    entry = f"{letter}{prog}"
    if use_names:
        name = _ascii_name(getattr(printer, "name", ""))
        if name:
            entry += _NAME_SEP + name
    return entry


def _natural_key(name):
    """Ключ натурального сортування: '2. A1' < '10. A1', числа рахуються як числа."""
    return [(0, int(t)) if t.isdigit() else (1, t.lower())
            for t in re.findall(r"\d+|\D+", str(name or ""))]


def build_frame(printers, use_names=False):
    """Будує кадр FW|<count>|<entry>,... Обрізає до 8 принтерів (стільки влазить на дисплей).

    Принтери сортуються натурально за назвою (1, 2, 3 ... а не порядок скрапу).
    use_names=True додає назву принтера в кожен запис (для показу назв замість номерів).
    """
    printers = sorted(list(printers or []), key=lambda p: _natural_key(getattr(p, "name", "")))
    shown = printers[:_MAX_PRINTERS]
    if len(printers) > _MAX_PRINTERS:
        logger.warning("📟 Дисплей: %d принтерів, показуємо перші %d", len(printers), _MAX_PRINTERS)
    entries = ",".join(_entry(p, use_names) for p in shown)
    return f"FW|{len(shown)}|{entries}\n"


class SerialDisplay:
    """Тримає останній кадр і раз на `heartbeat` секунд шле його у COM порт.

    Відправка окремим потоком, незалежно від повільного циклу монітора (10..60 с):
    ESP отримує стабільний heartbeat і завжди свіжий стан. Порт відкривається
    ліниво з backoff, помилки не валять farmwatch.

    Шлються лише СПРАВЖНІ дані монітора: до першого push і після того, як
    монітор замовк на stale_after секунд (клієнт закрили, ПК спав, WS відпав),
    відправка мовчить, і ESP чесно показує NO LINK замість застарілого стану.
    """

    def __init__(self, port, baud=115200, heartbeat=2.0, use_names=False,
                 serial_factory=None, stale_after=150.0):
        self.port = port
        self.baud = baud
        self.heartbeat = heartbeat
        self.use_names = use_names
        self.stale_after = stale_after  # None = слати вічно (як раніше)
        self._serial_factory = serial_factory  # інʼєкція для тестів; інакше pyserial
        self._ser = None
        self._last_frame = None         # нема даних, поки монітор не дав перший стан
        self._last_push = None
        self._lock = threading.Lock()
        self._thread = None
        self._running = False

    def push(self, printers):
        """Оновити останній відомий кадр. Викликається з колбека монітора, не блокує."""
        frame = build_frame(printers, self.use_names)
        with self._lock:
            self._last_frame = frame
            self._last_push = time.monotonic()

    def _frame_to_send(self):
        """Свіжий кадр або None, якщо даних ще нема / монітор давно мовчить."""
        with self._lock:
            frame = self._last_frame
            pushed = self._last_push
        if frame is None:
            return None
        if (self.stale_after is not None and pushed is not None
                and time.monotonic() - pushed > self.stale_after):
            return None
        return frame

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="SerialDisplay", daemon=True)
        self._thread.start()
        logger.info("📟 Дисплей: старт, порт %s @ %d", self.port, self.baud)

    def stop(self):
        self._running = False
        t = self._thread
        if t:
            t.join(timeout=self.heartbeat + 1)
        self._close()
        logger.info("📟 Дисплей: стоп")

    # --- внутрішнє ---

    def _pyserial_factory(self, port, baud):
        import serial  # ленивий імпорт: farmwatch не залежить від pyserial жорстко
        # write_timeout обовʼязковий: на завислому USB CDC (після сну ПК тощо)
        # write() без нього блокується вічно і потік дисплея тихо вмирає
        return serial.Serial(port, baud, timeout=1, write_timeout=2)

    def _resolve_port(self):
        """Порт з конфігу, або автопошук якщо він 'auto'/порожній. Перерішується щоразу."""
        if self.port and str(self.port).lower() != "auto":
            return self.port
        return autodetect_port()

    def _open(self):
        if self._ser is not None:
            return True
        port = self._resolve_port()
        if not port:
            logger.warning("📟 Дисплей: ESP не знайдено (автопошук)")
            return False
        factory = self._serial_factory or self._pyserial_factory
        try:
            self._ser = factory(port, self.baud)
            logger.info("📟 Дисплей: порт %s відкрито", port)
            return True
        except Exception as e:
            logger.warning("📟 Дисплей: не відкрити порт %s: %s", port, e)
            self._ser = None
            return False

    def _close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def _loop(self):
        backoff = 1.0
        while self._running:
            frame = self._frame_to_send()
            if frame is None:
                # мовчимо: без кадрів ESP сам перейде у NO LINK за кілька секунд
                time.sleep(self.heartbeat)
                continue
            if not self._open():
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            backoff = 1.0
            try:
                # без flush(): FlushFileBuffers на завислому CDC блокується вічно,
                # а кадр і так крихітний і повторюється кожен heartbeat
                self._ser.write(frame.encode("ascii", "ignore"))
            except Exception as e:
                logger.warning("📟 Дисплей: помилка запису: %s", e)
                self._close()
                continue
            time.sleep(self.heartbeat)


def _stale_after_from_config(config):
    """Поріг застарілості кадру: кілька пропущених циклів монітора з запасом."""
    try:
        interval = int((config or {}).get("monitor", {}).get("update_interval", 20))
    except (TypeError, ValueError, AttributeError):
        interval = 20
    return max(90.0, interval * 2.5 + 30.0)


def create_from_config(config):
    """Створити і запустити SerialDisplay якщо config['serial']['enabled']. Інакше None.

    Ніколи не кидає: помилка тут не має валити farmwatch.
    """
    try:
        cfg = (config or {}).get("serial", {}) if hasattr(config, "get") else {}
        if not cfg.get("enabled"):
            return None
        use_names = str(cfg.get("labels", "numbers")).lower() == "names"
        disp = SerialDisplay(cfg.get("port", "auto"), int(cfg.get("baud", 115200)),
                             use_names=use_names,
                             stale_after=_stale_after_from_config(config))
        disp.start()
        return disp
    except Exception as e:
        logger.warning("📟 Дисплей: не створити: %s", e)
        return None


def chain_push(monitor, display):
    """Дочепити display.push(printers) до наявного monitor.on_update_complete.

    Зберігає попередній колбек (бот/веб) і кличе обидва. No-op якщо display None.
    """
    if display is None:
        return
    prev = getattr(monitor, "on_update_complete", None)

    def _chained():
        if prev:
            prev()
        try:
            display.push(monitor.get_all_printers())
        except Exception as e:
            logger.warning("📟 Дисплей: push помилка: %s", e)

    monitor.on_update_complete = _chained


def apply_config(monitor, config):
    """Застосувати serial-конфіг НА ЛЬОТУ до запущеного монітора (без рестарту).

    Викликається при збереженні налаштувань: вмикає/вимикає або перевідкриває
    дисплей відповідно до config, якщо порт/baud/labels/enabled змінились.
    """
    cfg = (config or {}).get("serial", {}) if hasattr(config, "get") else {}
    disp = getattr(monitor, "_serial_display", None)
    if not cfg.get("enabled"):
        if disp is not None:
            try:
                disp.stop()
            except Exception:
                pass
            monitor._serial_display = None
            logger.info("📟 Дисплей вимкнено в налаштуваннях")
        return None

    port = cfg.get("port", "auto")
    baud = int(cfg.get("baud", 115200))
    use_names = str(cfg.get("labels", "numbers")).lower() == "names"
    if disp is not None:
        if disp.port == port and disp.baud == baud and disp.use_names == use_names:
            disp.stale_after = _stale_after_from_config(config)  # міг змінитись update_interval
            return disp  # без змін
        try:
            disp.stop()
        except Exception:
            pass
        monitor._serial_display = None
    logger.info("📟 Дисплей: застосовую налаштування (порт %s, labels %s)",
                port, "names" if use_names else "numbers")
    return attach(monitor, config)


def attach(monitor, config):
    """Єдина точка вбудовування дисплея. Ідемпотентно: один SerialDisplay на монітор.

    Інстанс зберігається на monitor._serial_display, тож бот і GUI не створять
    двох копій на один COM порт. Кличеться щоразу коли перевстановлюють
    on_update_complete (бот при кожному attach), щоб знову дочепити push.
    Ніколи не кидає. No-op якщо serial.enabled=false.
    """
    try:
        disp = getattr(monitor, "_serial_display", None)
        if disp is None:
            disp = create_from_config(config)
            if disp is None:
                return None
            monitor._serial_display = disp
        chain_push(monitor, disp)
        # одразу віддати поточний стан, не чекаючи наступного циклу монітора
        # (інакше після вмикання в налаштуваннях ESP до хвилини висить у NO LINK)
        try:
            printers = monitor.get_all_printers()
            if printers:
                disp.push(printers)
        except Exception:
            pass
        return disp
    except Exception as e:
        logger.warning("📟 Дисплей: не приєднано: %s", e)
        return None
