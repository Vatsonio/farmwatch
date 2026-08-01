"""Інтеграційні тести дисплея на ВІРТУАЛЬНОМУ ESP (без заліза).

Піднімає firmware/native_test/virtual_esp.exe — це справжня прошивка,
скомпільована під ПК, — і годує її через справжній SerialDisplay. Тобто
перевіряється весь ланцюг: PrinterStatus -> кадр FW| -> парсер прошивки ->
текст на матриці.

Потрібен g++ у PATH; без нього тести пропускаються.
"""

import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from serial_output import SerialDisplay

FW = Path(__file__).parent / "firmware"
SRC = FW / "native_test" / "virtual_esp.cpp"
STUBS = FW / "native_test" / "stubs"
EXE = Path(os.environ.get("TEMP", ".")) / "farmwatch_virtual_esp.exe"

# Час прошивки біжить у 30 разів швидше: 5 хв до самоперезавантаження це 10с
# реального часу. Heartbeat дисплея беремо 0.05с (= 1.5с часу прошивки), щоб
# кадри впевнено встигали в межі 6-секундного таймауту звʼязку.
TIME_SCALE = "30"
HEARTBEAT = 0.05


def _build():
    if shutil.which("g++") is None:
        return False
    r = subprocess.run(["g++", "-std=c++17", "-I", str(STUBS), str(SRC), "-o", str(EXE)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[:2000], file=sys.stderr)
        return False
    return True


_BUILT = _build()


def _p(status, progress, name="", online=True, serial=""):
    return SimpleNamespace(status=status, progress=progress, online=online,
                           name=name, serial=serial)


class VirtualEsp:
    """Плата як підпроцес + інтерфейс serial-порту для SerialDisplay."""

    def __init__(self, time_scale=TIME_SCALE):
        env = dict(os.environ, FW_TIME_SCALE=time_scale)
        self.proc = subprocess.Popen([str(EXE)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, env=env, bufsize=0)
        self.lines = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        self.wait_line(lambda s: s == "READY", timeout=10)

    def _pump(self):
        for raw in iter(self.proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                with self._lock:
                    self.lines.append(line)

    # --- те, що бачить SerialDisplay (як pyserial) ---
    def write(self, b):
        self.proc.stdin.write(b)
        self.proc.stdin.flush()
        return len(b)

    def flush(self):
        pass

    def close(self):
        # Закриття COM порту НЕ вимикає плату: вона живе далі і має сама
        # перейти в NO LINK. Процес завершує тест у tearDown.
        pass

    def shutdown(self):
        for step in (self.proc.kill, self.proc.wait,
                     self.proc.stdin.close, self.proc.stdout.close):
            try:
                step()
            except Exception:
                pass
        self._reader.join(timeout=2)

    # --- спостереження ---
    def snapshot(self):
        with self._lock:
            return list(self.lines)

    def mark(self) -> int:
        """Позиція у логу: далі чекаємо лише те, що станеться ПІСЛЯ неї."""
        with self._lock:
            return len(self.lines)

    def reboots(self, since=0):
        return sum(1 for s in self.snapshot()[since:] if s == "REBOOT")

    def wait_line(self, pred, timeout=10, since=0):
        end = time.time() + timeout
        while time.time() < end:
            for s in self.snapshot()[since:]:
                if pred(s):
                    return s
            time.sleep(0.02)
        return None

    def wait_screen(self, needle, timeout=10, since=0):
        got = self.wait_line(lambda s: s.startswith("SCREEN ") and needle in s,
                             timeout, since)
        return got[7:] if got else None


@unittest.skipUnless(_BUILT, "потрібен g++ щоб зібрати віртуальний ESP")
class VirtualEspTests(unittest.TestCase):
    def setUp(self):
        self.esp = VirtualEsp()
        self.disp = None

    def tearDown(self):
        if self.disp is not None:
            self.disp.stop()
        self.esp.shutdown()

    def _display(self, heartbeat=HEARTBEAT, **kw):
        self.disp = SerialDisplay("VIRT", heartbeat=heartbeat,
                                  serial_factory=lambda port, baud: self.esp, **kw)
        return self.disp

    def test_shows_no_link_before_any_data(self):
        # плата щойно ввімкнулась, farmwatch мовчить: одразу NO LINK і жодних
        # порожніх зведень типу "PRINT 0 ERR 0 IDLE 0 DONE 0"
        self.assertIsNotNone(self.esp.wait_screen("NO LINK", timeout=5))
        self.assertEqual([s for s in self.esp.snapshot()
                          if s.startswith("SCREEN ") and "PRINT" in s], [])

    def test_summary_counts_match_the_farm(self):
        disp = self._display(use_names=True)
        disp.push([
            _p("printing", 80, "1. mini"), _p("printing", 75, "1. A1"),
            _p("printing", 46, "2. A1"), _p("idle", 0, "3. A1"),
            _p("idle", 0, "4. A1"), _p("idle", 0, "5. A1"), _p("idle", 0, "6. A1"),
        ])
        disp.start()
        screen = self.esp.wait_screen("PRINT 3", timeout=10)
        self.assertEqual(screen, "PRINT 3  ERR 0  IDLE 4  DONE 0")

    def test_detail_screen_shows_names_and_progress(self):
        disp = self._display(use_names=True)
        disp.push([_p("printing", 72, "1. mini"), _p("idle", 0, "2. A1")])
        disp.start()
        detail = self.esp.wait_screen("1. mini", timeout=10)
        self.assertIsNotNone(detail)
        self.assertIn("72%", detail)
        self.assertIn("2. A1 IDLE", detail)

    def test_detail_screen_shows_numbers_without_names(self):
        disp = self._display(use_names=False)
        disp.push([_p("printing", 33, "1. A1"), _p("idle", 0, "2. A1")])
        disp.start()
        detail = self.esp.wait_screen("33%", timeout=10)
        self.assertIsNotNone(detail)
        self.assertTrue(detail.startswith("1 33%"), detail)

    def test_finished_print_pops_event_with_printer_name(self):
        disp = self._display(use_names=True)
        disp.push([_p("printing", 99, "1. mini"), _p("printing", 50, "2. A1")])
        disp.start()
        self.assertIsNotNone(self.esp.wait_screen("PRINT 2", timeout=10))
        disp.push([_p("finished", 100, "1. mini"), _p("printing", 51, "2. A1")])
        self.assertIsNotNone(self.esp.wait_screen("DONE 1. mini", timeout=10))

    def test_link_lost_falls_back_to_no_link(self):
        # farmwatch перестав слати кадри -> плата чесно каже NO LINK,
        # а не малює вічно застарілий стан ферми
        disp = self._display(use_names=True)
        disp.push([_p("printing", 80, "1. mini")])
        disp.start()
        self.assertIsNotNone(self.esp.wait_screen("PRINT 1", timeout=10))
        mark = self.esp.mark()          # усе далі має статись ПІСЛЯ цієї точки
        disp.stop()
        self.disp = None
        self.assertIsNotNone(self.esp.wait_screen("NO LINK", timeout=10, since=mark))

    def test_reboots_once_after_link_loss_not_in_a_loop(self):
        # був звʼязок і зник: рівно одне самоперезавантаження, далі спокій
        disp = self._display(use_names=True)
        disp.push([_p("printing", 80, "1. mini")])
        disp.start()
        self.assertIsNotNone(self.esp.wait_screen("PRINT 1", timeout=10))
        mark = self.esp.mark()
        disp.stop()
        self.disp = None
        # 5 хв часу прошивки = 10с реального при FW_TIME_SCALE=30
        self.assertIsNotNone(self.esp.wait_line(lambda s: s == "REBOOT",
                                                timeout=25, since=mark))
        time.sleep(15)  # ще 7 хв часу прошивки: другого ребуту бути не має
        self.assertEqual(self.esp.reboots(since=mark), 1)

    def test_no_reboot_loop_when_farmwatch_never_ran(self):
        # плату ввімкнули, farmwatch не запущений: вічного циклу ребутів нема
        time.sleep(25)  # 12+ хв часу прошивки
        self.assertEqual(self.esp.reboots(), 0)
        self.assertIsNotNone(self.esp.wait_screen("NO LINK", timeout=5))

    def test_recovers_after_reboot_when_data_returns(self):
        # після самоперезавантаження плата має знову підхопити кадри
        disp = self._display(use_names=True)
        disp.push([_p("printing", 80, "1. mini")])
        disp.start()
        self.assertIsNotNone(self.esp.wait_screen("PRINT 1", timeout=10))
        mark = self.esp.mark()
        disp.stop()
        self.assertIsNotNone(self.esp.wait_line(lambda s: s == "REBOOT",
                                                timeout=25, since=mark))
        after = self.esp.mark()
        disp.push([_p("printing", 10, "1. mini"), _p("printing", 20, "2. A1")])
        disp.start()
        self.assertIsNotNone(self.esp.wait_screen("PRINT 2", timeout=10, since=after))

    def test_eight_printers_with_long_names(self):
        disp = self._display(use_names=True)
        disp.push([_p("printing", 50, f"{i}. LongPrinter") for i in range(1, 9)])
        disp.start()
        screen = self.esp.wait_screen("PRINT 8", timeout=10)
        self.assertEqual(screen, "PRINT 8  ERR 0  IDLE 0  DONE 0")
        detail = self.esp.wait_screen("8. LongPrinter", timeout=10)
        self.assertIsNotNone(detail)

    def test_offline_and_paused_are_shown(self):
        disp = self._display(use_names=True)
        disp.push([_p("printing", 10, "1. A1", online=False),
                   _p("paused", 40, "2. A1"), _p("stopped", 0, "3. A1")])
        disp.start()
        screen = self.esp.wait_screen("ERR 1", timeout=10)
        self.assertIsNotNone(screen)
        self.assertIn("PAUSE 1", screen)
        self.assertIn("OFF 1", screen)


if __name__ == "__main__":
    unittest.main()
