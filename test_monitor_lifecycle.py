"""Тести життєвого циклу монітора: стоп має прибирати за собою ВСЕ.

Без клієнта і без заліза: CDP-виклики або падають (і глушаться), або
підмінені. Ці тести ловлять саме ті регресії, через які рестарт лишав
живий дисплей на COM порту, живий потік циклу і зайві вікна клієнта.
"""

import threading
import time
import unittest
from types import SimpleNamespace

from printer_monitor import (BambuPrinterMonitor, DASHBOARD_TITLE,
                             DASHBOARD_WINDOW_NAME)


def _mon(**kw):
    kw.setdefault("auto_launch", False)
    m = BambuPrinterMonitor(**kw)
    m._list_pages = lambda: []          # без живого клієнта
    return m


def _page(pid, title="index.html#/monitor", url="file:///app/index.html#/monitor",
          window_name=""):
    return {"id": pid, "type": "page", "webSocketDebuggerUrl": f"ws://{pid}",
            "title": title, "url": url, "_window_name": window_name}


def _with_window_names(m):
    """Підмінити CDP-евал window.name даними зі сторінки (без живого клієнта)."""
    pages = {}

    def fake_eval(ws_url, expression, timeout=8.0):
        if "window.name" not in expression:
            return None
        pid = ws_url.replace("ws://", "")
        return {"result": {"result": {"value": pages.get(pid, "")}}}

    def set_pages(page_list):
        pages.clear()
        pages.update({p["id"]: p.get("_window_name", "") for p in page_list})
        m._list_pages = lambda: page_list

    m._ws_eval = fake_eval
    return set_pages


class FakeDisplay:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class StopTeardownTests(unittest.TestCase):
    def test_stop_stops_serial_display(self):
        # дисплей мусить віддати COM порт, інакше наступний монітор його не відкриє
        m = _mon()
        disp = FakeDisplay()
        m._serial_display = disp
        m.stop()
        self.assertTrue(disp.stopped)
        self.assertIsNone(getattr(m, "_serial_display"))

    def test_stop_interrupts_long_sleep_fast(self):
        # цикл спить update_interval; stop() має будити його одразу, інакше
        # старий потік доживає і конкурує з новим монітором
        m = _mon(update_interval=300)
        m.update_printers = lambda: True
        m.running = True
        m._stop_event.clear()
        m.monitor_thread = threading.Thread(target=m._monitor_loop, daemon=True)
        m.monitor_thread.start()
        time.sleep(0.3)
        t0 = time.monotonic()
        m.stop()
        elapsed = time.monotonic() - t0
        self.assertFalse(m.monitor_thread.is_alive())
        self.assertLess(elapsed, 5)

    def test_stop_closes_own_dashboard_windows(self):
        # закриваємо СВОЇ вікна (за id і за міткою window.name), чуже вікно
        # користувача не чіпаємо навіть якщо воно теж на #/monitor
        m = _mon()
        m._dashboard_target_id = "MINE"
        set_pages = _with_window_names(m)
        set_pages([
            _page("MINE"),                                    # наше (за id)
            _page("ZOMBIE", window_name=DASHBOARD_WINDOW_NAME),  # від старого запуску
            _page("USER"),                                    # користувача
        ])
        closed = []
        m._close_target = lambda tid: (closed.append(tid), True)[1]
        m.stop()
        self.assertEqual(sorted(closed), ["MINE", "ZOMBIE"])

    def test_stop_keeps_user_window_when_it_was_only_borrowed(self):
        # чуже вікно ми лише читали: закривати його при зупинці не можна
        m = _mon()
        m._dashboard_target_id = None
        set_pages = _with_window_names(m)
        set_pages([_page("USER")])
        closed = []
        m._close_target = lambda tid: (closed.append(tid), True)[1]
        m.stop()
        self.assertEqual(closed, [])

    def test_title_alone_is_not_ownership(self):
        # роутер клієнта сам ставить/перезаписує title, тож він не є ознакою
        m = _mon()
        set_pages = _with_window_names(m)
        set_pages([_page("X", title=DASHBOARD_TITLE)])  # титул є, мітки нема
        self.assertEqual(m._our_monitor_targets(), [])

    def test_stop_is_safe_without_anything_started(self):
        _mon().stop()  # не має кидати


class RestartTests(unittest.TestCase):
    def test_reconnect_does_nothing_while_stopping(self):
        # під час зупинки перепідключення не має воскрешати зʼєднання
        # (і тим паче піднімати клієнт)
        m = _mon()
        m.running = False
        called = []
        m._ensure_client_ready = lambda: called.append(1) or True
        self.assertFalse(m._reconnect())
        self.assertEqual(called, [])

    def test_start_refuses_while_old_loop_alive(self):
        # два цикли одночасно = дублікати вікон і бійка за CDP
        m = _mon(update_interval=300)
        stuck = threading.Event()
        m.monitor_thread = threading.Thread(target=lambda: stuck.wait(30), daemon=True)
        m.monitor_thread.start()
        started = []
        m._ensure_client_ready = lambda: started.append(1) or True
        try:
            self.assertFalse(m.start())
            self.assertEqual(started, [])  # навіть не намагався піднімати клієнт
        finally:
            stuck.set()

    def test_ensure_client_ready_restarts_client_without_debug_port(self):
        # клієнт живий, але без debug-порту: Electron не віддасть порт новому
        # процесу, тож спершу треба закрити наявний
        m = _mon(auto_launch=True)
        calls = []
        m.is_app_running = lambda: False
        m._is_process_running = lambda: True
        m._terminate_running_app = lambda: calls.append("kill") or True
        m.launch_app = lambda: calls.append("launch") or True
        self.assertTrue(m._ensure_client_ready())
        self.assertEqual(calls, ["kill", "launch"])

    def test_ensure_client_ready_without_auto_launch(self):
        m = _mon(auto_launch=False)
        m.is_app_running = lambda: False
        self.assertFalse(m._ensure_client_ready())


class DashboardChoiceTests(unittest.TestCase):
    def test_prefers_own_window_and_closes_duplicates(self):
        m = _mon()
        set_pages = _with_window_names(m)
        set_pages([
            _page("USER"),
            _page("OURS", window_name=DASHBOARD_WINDOW_NAME),
            _page("OLD", window_name=DASHBOARD_WINDOW_NAME),
        ])
        closed = []
        m._close_target = lambda tid: (closed.append(tid), True)[1]
        page = m.get_dashboard_page()
        self.assertEqual(page["id"], "OURS")   # своє вікно, не користувацьке
        self.assertEqual(closed, ["OLD"])      # зайвий дубль прибрано
        self.assertNotIn("USER", closed)       # чуже не чіпаємо

    def test_borrowed_user_window_is_not_claimed(self):
        # свого нема і відкрити не вийшло: працюємо у вікні користувача,
        # але не записуємо його у власні (щоб не закрити при зупинці)
        m = _mon()
        set_pages = _with_window_names(m)
        set_pages([_page("USER")])
        m._open_monitor_window = lambda main: None
        page = m.get_dashboard_page()
        self.assertEqual(page["id"], "USER")
        self.assertIsNone(m._dashboard_target_id)
        self.assertEqual(m._our_monitor_targets(), [])


if __name__ == "__main__":
    unittest.main()
