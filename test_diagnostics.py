"""
Тести діагностики зникнення принтерів (parse-статистика + _diagnose_cycle).
Не потребують live-дашборда чи Telegram — годуємо монітор синтетичним HTML.

Запуск:  python test_diagnostics.py
"""

import logging
import os
import shutil
import tempfile

from printer_monitor import BambuPrinterMonitor


# --- допоміжне: збираємо лог-рядки монітора ---
class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self):
        return [r.getMessage() for r in self.records]


def _card(name=None, status=None, file_name="bench.3mf"):
    """Картка принтера у форматі, який очікує parse_printers_from_html."""
    name_div = f'<div class="monitor_printer-name">{name}</div>' if name is not None else ""
    status_div = f'<div class="monitor_printer-status">{status}</div>' if status is not None else ""
    return (
        '<div class="monitor_printer">'
        f'{name_div}'
        f'<div class="monitor_printing_file">{file_name}</div>'
        f'{status_div}'
        '</div>'
    )


def _page(cards):
    return "<html><body>" + "".join(cards) + "</body></html>"


def _new_monitor(dump_dir):
    return BambuPrinterMonitor(
        auto_launch=False,
        debug_logging=True,
        debug_dump_dir=dump_dir,
    )


def test_parse_stats_counts():
    """Парсер рахує знайдені/розпарсені/скіпнуті картки."""
    tmp = tempfile.mkdtemp()
    try:
        m = _new_monitor(tmp)
        html = _page([
            _card("P1", "printing 50% -1h"),
            _card("P2", "idle"),
            _card("P4", None),          # без статусу -> skip
            _card(None, "idle"),        # без назви   -> skip
        ])
        printers = m.parse_printers_from_html(html)
        st = m._last_parse_stats

        assert st["cards_found"] == 4, st
        assert st["parsed_ok"] == 2, st
        assert st["skipped_no_status"] == 1, st
        assert st["skipped_no_name"] == 1, st
        assert "P4" in st["skipped_names"], st
        assert len(printers) == 2
        print("OK  test_parse_stats_counts")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_status_parsing():
    """Статус/прогрес визначаються коректно."""
    tmp = tempfile.mkdtemp()
    try:
        m = _new_monitor(tmp)
        printers = m.parse_printers_from_html(_page([
            _card("Printing", "printing 73% -2h11m"),
            _card("Idle", "idle"),
            _card("Done", "finished 100%"),
            _card("Paused", "paused 45%"),
        ]))
        by = {p.name: p for p in printers}
        assert by["Printing"].status == "printing" and by["Printing"].progress == 73
        assert by["Idle"].status == "idle"
        assert by["Done"].status == "finished"
        assert by["Paused"].status == "paused" and by["Paused"].progress == 45
        print("OK  test_status_parsing")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_vanish_detection_and_dump():
    """Зникнення принтера між циклами фіксується + зберігається HTML-дамп."""
    tmp = tempfile.mkdtemp()
    handler = ListHandler()
    log = logging.getLogger("printer_monitor")
    log.addHandler(handler)
    try:
        m = _new_monitor(tmp)

        # Цикл 1: три принтери
        html1 = _page([
            _card("P1", "printing 50% -1h"),
            _card("P2", "idle"),
            _card("P3", "printing 10% -2h"),
        ])
        ps1 = m.parse_printers_from_html(html1)
        m._diagnose_cycle(ps1, html1)
        for p in ps1:
            m._apply_printer_update(p)
        assert set(m.printers.keys()) == {"P1", "P2", "P3"}

        handler.records.clear()

        # Цикл 2: P2 зник зі scrape
        html2 = _page([
            _card("P1", "printing 55% -1h"),
            _card("P3", "printing 12% -2h"),
        ])
        ps2 = m.parse_printers_from_html(html2)
        m._diagnose_cycle(ps2, html2)

        msgs = handler.messages()
        assert any("ЗНИКЛИ" in msg and "P2" in msg for msg in msgs), msgs
        assert any("впала: 3" in msg for msg in msgs), msgs

        dumps = [f for f in os.listdir(tmp) if f.startswith("dashboard_")]
        assert dumps, "очікувався HTML-дамп при аномалії"

        # принтер лишається у пам'яті (монотонність словника) — це і є корінь "зникнення"
        assert "P2" in m.printers, "P2 не має зникати зі словника, лише зі scrape"
        print("OK  test_vanish_detection_and_dump")
    finally:
        log.removeHandler(handler)
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_false_vanish_on_stable():
    """Стабільний набір принтерів не дає хибних 'ЗНИКЛИ'."""
    tmp = tempfile.mkdtemp()
    handler = ListHandler()
    log = logging.getLogger("printer_monitor")
    log.addHandler(handler)
    try:
        m = _new_monitor(tmp)
        cards = [_card("A", "printing 5% -9h"), _card("B", "idle")]
        for _ in range(3):
            ps = m.parse_printers_from_html(_page(cards))
            m._diagnose_cycle(ps, _page(cards))
            for p in ps:
                m._apply_printer_update(p)
        assert not any("ЗНИКЛИ" in msg for msg in handler.messages()), handler.messages()
        print("OK  test_no_false_vanish_on_stable")
    finally:
        log.removeHandler(handler)
        shutil.rmtree(tmp, ignore_errors=True)


def test_zero_cards_anomaly():
    """Порожній scrape (0 карток) фіксується як аномалія."""
    tmp = tempfile.mkdtemp()
    handler = ListHandler()
    log = logging.getLogger("printer_monitor")
    log.addHandler(handler)
    try:
        m = _new_monitor(tmp)
        # спершу наповнюємо
        ps = m.parse_printers_from_html(_page([_card("A", "idle")]))
        m._diagnose_cycle(ps, _page([_card("A", "idle")]))
        handler.records.clear()
        # тепер порожня сторінка
        empty = _page([])
        ps0 = m.parse_printers_from_html(empty)
        m._diagnose_cycle(ps0, empty)
        assert any("0 карток" in msg for msg in handler.messages()), handler.messages()
        print("OK  test_zero_cards_anomaly")
    finally:
        log.removeHandler(handler)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_parse_stats_counts()
    test_status_parsing()
    test_vanish_detection_and_dump()
    test_no_false_vanish_on_stable()
    test_zero_cards_anomaly()
    print("\nALL TESTS PASSED")
    # прибираємо debug-лог, створений у cwd під час тестів
    try:
        if os.path.exists("printer_monitor.debug.log"):
            os.remove("printer_monitor.debug.log")
    except OSError:
        pass
