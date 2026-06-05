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


def _mon_card(name, status_text, nozzle="0/0°C", bed="0/0°C", speed="Standard", file="job.3mf"):
    """Картка у форматі реального дашборда Bambu (роут #/monitor)."""
    return (
        '<div class="monitor_printer h-full p-2 flex gap-1 flex-col text-xs border rounded-[4px] min-h-32">'
        f'<div class="monitor_printer-name overflow-hidden text-ellipsis line-clamp-2 break-words">{name}</div>'
        f'<div class="flex-grow overflow-hidden"><div class="monitor_printing_file text-gray-700">{file}</div></div>'
        '<div class="text-[10px]">'
        f'<div class="flex items-center gap-1 text-gray-700"><span>{speed}</span></div>'
        '<div class="text-gray-700 flex items-center gap-2">'
        f'<div class="flex items-center gap-1"><span>{nozzle}</span></div>'
        f'<div class="flex items-center gap-1"><span>{bed}</span><span></span></div>'
        '</div></div>'
        f'<div class="monitor_printer-status -m-2 -mt-0 py-[2px] text-center text-sm">{status_text}</div>'
        '</div>'
    )


def test_real_dashboard_markup():
    """Реальна розмітка #/monitor: статуси printing/finished/idle і модель з дужок."""
    tmp = tempfile.mkdtemp()
    try:
        m = _new_monitor(tmp)
        html = '<div class="monitor_printers monitor_printers--large">' + "".join([
            _mon_card("1. (A1 mini)", "60% -1h36m", nozzle="245/245°C", bed="80/80°C"),
            _mon_card("6. (A1)", "Finished", nozzle="52/--°C", bed="52/--°C"),
            _mon_card("3. (A1)", "Idle", nozzle="31/--°C", bed="29/--°C"),
        ]) + "</div>"
        printers = {p.name: p for p in m.parse_printers_from_html(html)}
        assert len(printers) == 3, printers

        a = printers["1. (A1 mini)"]
        assert a.status == "printing", a.status
        assert a.progress == 60, a.progress
        assert a.remaining_time == "-1h36m", a.remaining_time
        assert a.model == "A1 mini", a.model
        assert a.nozzle_temp == "245/245°C", a.nozzle_temp

        assert printers["6. (A1)"].status == "finished", printers["6. (A1)"].status
        assert printers["3. (A1)"].status == "idle", printers["3. (A1)"].status
        assert printers["3. (A1)"].model == "A1", printers["3. (A1)"].model
        print("OK  test_real_dashboard_markup")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_parse_stats_counts()
    test_status_parsing()
    test_real_dashboard_markup()
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
