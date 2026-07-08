"""Демо-подача кадрів у ESP матрицю без farmwatch.

Симулює ферму: принтери друкують, завершуються, інколи стоп/пауза/offline.
Дає перевірити прошивку і розводку на реальному ESP.

Приклад:
    python feed_demo.py COM3
    python feed_demo.py COM3 --printers 5 --interval 0.5
    python feed_demo.py - --print --cycles 20   # без порту, кадри у stdout
"""

import argparse
import logging
import random
import time
from types import SimpleNamespace

from serial_output import SerialDisplay, build_frame

logging.basicConfig(level=logging.INFO, format="%(message)s")


class _NullSerial:
    """Заглушка для режиму без порту (--print / port '-')."""
    def write(self, b):
        return len(b)

    def flush(self):
        pass

    def close(self):
        pass


def make_printers(n):
    return [SimpleNamespace(status="printing", progress=random.randint(0, 60), online=True)
            for _ in range(n)]


def advance(printers):
    for p in printers:
        if p.status == "printing":
            p.progress += random.randint(3, 9)
            if p.progress >= 100:
                p.progress = 100
                p.status = "finished"
            elif random.random() < 0.02:
                p.status = "stopped"        # рідкісна помилка
            elif random.random() < 0.02:
                p.status = "paused"
        elif p.status == "finished":
            if random.random() < 0.3:       # забрали друк, новий старт
                p.status, p.progress = "printing", 0
        elif p.status in ("stopped", "paused"):
            if random.random() < 0.3:
                p.status = "printing"
        elif p.status == "idle":
            if random.random() < 0.2:
                p.status, p.progress = "printing", 0
        if random.random() < 0.01:
            p.online = not p.online


def main():
    ap = argparse.ArgumentParser(description="Демо-подача кадрів у ESP матрицю")
    ap.add_argument("port", nargs="?", default="auto",
                    help="COM порт (напр. COM4), 'auto' автопошук (дефолт), '-' без порту")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--printers", type=int, default=5)
    ap.add_argument("--interval", type=float, default=1.0, help="секунд між змінами стану")
    ap.add_argument("--cycles", type=int, default=0, help="скільки кроків (0 = нескінченно)")
    ap.add_argument("--print", dest="echo", action="store_true", help="друкувати кадри у stdout")
    args = ap.parse_args()

    dry = args.port == "-"
    factory = (lambda port, baud: _NullSerial()) if dry else None
    disp = SerialDisplay(args.port, args.baud, heartbeat=2.0, serial_factory=factory)
    disp.start()

    printers = make_printers(args.printers)
    step = 0
    try:
        while args.cycles == 0 or step < args.cycles:
            disp.push(printers)
            if args.echo or dry:
                print(build_frame(printers).rstrip("\n"))
            advance(printers)
            step += 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        disp.stop()


if __name__ == "__main__":
    main()
