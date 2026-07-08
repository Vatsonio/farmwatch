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


def _entry(printer):
    """Один принтер у вигляді <letter><progress>. Duck typing по PrinterStatus."""
    status = getattr(printer, "status", None) or "idle"
    if not getattr(printer, "online", True):
        status = "offline"
    letter = _STATUS_LETTER.get(status, _DEFAULT_LETTER)
    try:
        prog = int(getattr(printer, "progress", 0) or 0)
    except (TypeError, ValueError):
        prog = 0
    prog = max(0, min(100, prog))
    return f"{letter}{prog}"


def build_frame(printers):
    """Будує кадр FW|<count>|<entry>,... Обрізає до 8 принтерів (стільки колонок на 8x8)."""
    printers = list(printers or [])
    shown = printers[:_MAX_PRINTERS]
    if len(printers) > _MAX_PRINTERS:
        logger.warning("📟 Дисплей: %d принтерів, показуємо перші %d", len(printers), _MAX_PRINTERS)
    entries = ",".join(_entry(p) for p in shown)
    return f"FW|{len(shown)}|{entries}\n"


class SerialDisplay:
    """Тримає останній кадр і раз на `heartbeat` секунд шле його у COM порт.

    Відправка окремим потоком, незалежно від повільного циклу монітора (10..60 с):
    ESP отримує стабільний heartbeat і завжди свіжий стан. Порт відкривається
    ліниво з backoff, помилки не валять farmwatch.
    """

    def __init__(self, port, baud=115200, heartbeat=2.0, serial_factory=None):
        self.port = port
        self.baud = baud
        self.heartbeat = heartbeat
        self._serial_factory = serial_factory  # інʼєкція для тестів; інакше pyserial
        self._ser = None
        self._last_frame = "FW|0|\n"
        self._lock = threading.Lock()
        self._thread = None
        self._running = False

    def push(self, printers):
        """Оновити останній відомий кадр. Викликається з колбека монітора, не блокує."""
        frame = build_frame(printers)
        with self._lock:
            self._last_frame = frame

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
        return serial.Serial(port, baud, timeout=1)

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
            if not self._open():
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            backoff = 1.0
            with self._lock:
                frame = self._last_frame
            try:
                self._ser.write(frame.encode("ascii", "ignore"))
                flush = getattr(self._ser, "flush", None)
                if callable(flush):
                    flush()
            except Exception as e:
                logger.warning("📟 Дисплей: помилка запису: %s", e)
                self._close()
                continue
            time.sleep(self.heartbeat)


def create_from_config(config):
    """Створити і запустити SerialDisplay якщо config['serial']['enabled']. Інакше None.

    Ніколи не кидає: помилка тут не має валити farmwatch.
    """
    try:
        cfg = (config or {}).get("serial", {}) if hasattr(config, "get") else {}
        if not cfg.get("enabled"):
            return None
        disp = SerialDisplay(cfg.get("port", "COM3"), int(cfg.get("baud", 115200)))
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
        return disp
    except Exception as e:
        logger.warning("📟 Дисплей: не приєднано: %s", e)
        return None
