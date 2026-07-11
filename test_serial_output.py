"""Тести формату кадру і відправника SerialDisplay. Без заліза (fake serial)."""

import time
import unittest
from types import SimpleNamespace

import serial_output
from serial_output import build_frame, SerialDisplay, autodetect_port, _ascii_name


def _p(status, progress, online=True, name=""):
    return SimpleNamespace(status=status, progress=progress, online=online, name=name)


def _port(device, vid):
    return SimpleNamespace(device=device, vid=vid)


class FakeSerial:
    def __init__(self, fail_writes=False):
        self.writes = []
        self.closed = False
        self.fail_writes = fail_writes

    def write(self, b):
        if self.fail_writes:
            raise OSError("порт завис")
        self.writes.append(b)
        return len(b)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class BuildFrameTests(unittest.TestCase):
    def test_basic_mapping(self):
        printers = [_p("printing", 72), _p("idle", 0), _p("stopped", 0), _p("finished", 100)]
        self.assertEqual(build_frame(printers), "FW|4|p72,i0,e0,d100\n")

    def test_all_states(self):
        printers = [_p("printing", 5), _p("paused", 40), _p("finished", 100),
                    _p("stopped", 0), _p("idle", 0)]
        self.assertEqual(build_frame(printers), "FW|5|p5,a40,d100,e0,i0\n")

    def test_offline_overrides_status(self):
        self.assertEqual(build_frame([_p("printing", 50, online=False)]), "FW|1|o50\n")

    def test_progress_clamped(self):
        self.assertEqual(build_frame([_p("printing", 250), _p("printing", -5)]),
                         "FW|2|p100,p0\n")

    def test_bad_progress_defaults_zero(self):
        self.assertEqual(build_frame([_p("printing", None), _p("printing", "x")]),
                         "FW|2|p0,p0\n")

    def test_unknown_status_defaults_idle(self):
        self.assertEqual(build_frame([_p("wtf", 30)]), "FW|1|i30\n")

    def test_empty(self):
        self.assertEqual(build_frame([]), "FW|0|\n")
        self.assertEqual(build_frame(None), "FW|0|\n")

    def test_truncates_to_eight(self):
        printers = [_p("printing", i) for i in range(10)]
        frame = build_frame(printers)
        self.assertTrue(frame.startswith("FW|8|"))
        self.assertEqual(frame.count(","), 7)


class NameTests(unittest.TestCase):
    def test_default_has_no_names(self):
        self.assertEqual(build_frame([_p("printing", 50, name="A1")]), "FW|1|p50\n")

    def test_use_names_appends_name(self):
        printers = [_p("printing", 72, name="A1"), _p("idle", 0, name="X1C")]
        self.assertEqual(build_frame(printers, use_names=True), "FW|2|p72\x1fA1,i0\x1fX1C\n")

    def test_use_names_missing_name_omits_sep(self):
        self.assertEqual(build_frame([_p("printing", 5, name="")], use_names=True), "FW|1|p5\n")

    def test_ascii_name_strips_non_ascii_and_commas(self):
        # кирилиця відкидається (шрифт латиниця), кома прибирається
        self.assertEqual(_ascii_name("Принтер A1, right"), "A1 right")

    def test_ascii_name_collapses_and_truncates(self):
        self.assertEqual(_ascii_name("  Bambu   X1  Carbon  "), "Bambu X1 Carbon")
        self.assertEqual(_ascii_name("X" * 40), "X" * 16)

    def test_ascii_name_removes_separator_char(self):
        self.assertNotIn("\x1f", _ascii_name("A1\x1fB2"))


class AutodetectTests(unittest.TestCase):
    def test_prefers_espressif_vid(self):
        ports = [_port("COM20", None), _port("COM4", 0x303A), _port("COM5", 0x1A86)]
        self.assertEqual(autodetect_port(ports), "COM4")

    def test_falls_back_to_bridge(self):
        ports = [_port("COM20", None), _port("COM7", 0x10C4)]
        self.assertEqual(autodetect_port(ports), "COM7")

    def test_none_when_no_match(self):
        ports = [_port("COM20", None), _port("COM21", 0x1234)]
        self.assertIsNone(autodetect_port(ports))

    def test_resolve_explicit_port_wins(self):
        d = SerialDisplay("COM9")
        self.assertEqual(d._resolve_port(), "COM9")

    def test_resolve_auto_uses_autodetect(self):
        d = SerialDisplay("auto")
        # без заліза autodetect поверне None або якийсь порт; головне, що не 'auto'
        self.assertNotEqual(d._resolve_port(), "auto")


class SenderTests(unittest.TestCase):
    def test_writes_pushed_frame(self):
        fake = FakeSerial()
        disp = SerialDisplay("COMX", baud=115200, heartbeat=0.05,
                             serial_factory=lambda port, baud: fake)
        disp.push([_p("printing", 33), _p("idle", 0)])
        disp.start()
        time.sleep(0.2)
        disp.stop()
        written = b"".join(fake.writes).decode("ascii")
        self.assertIn("FW|2|p33,i0\n", written)
        self.assertTrue(fake.closed)

    def test_survives_port_open_failure(self):
        def boom(port, baud):
            raise OSError("порт зайнятий")
        disp = SerialDisplay("COMX", heartbeat=0.05, serial_factory=boom)
        disp.push([_p("idle", 0)])
        disp.start()
        time.sleep(0.15)
        disp.stop()  # не має кинути виняток

    def test_silent_before_first_push(self):
        # до першого push нема чого казати ESP: мовчимо, ESP покаже NO LINK
        fake = FakeSerial()
        disp = SerialDisplay("COMX", heartbeat=0.05,
                             serial_factory=lambda port, baud: fake)
        disp.start()
        time.sleep(0.2)
        disp.stop()
        self.assertEqual(fake.writes, [])

    def test_stops_sending_when_stale(self):
        # монітор замовк (клієнт умер, ПК спав) -> перестаємо слати застарілий
        # кадр, ESP чесно показує NO LINK замість вчорашнього стану
        fake = FakeSerial()
        disp = SerialDisplay("COMX", heartbeat=0.05, stale_after=0.15,
                             serial_factory=lambda port, baud: fake)
        disp.push([_p("printing", 33)])
        disp.start()
        time.sleep(0.1)
        n_early = len(fake.writes)
        time.sleep(0.4)
        n_late = len(fake.writes)
        disp.stop()
        self.assertGreater(n_early, 0)
        self.assertLessEqual(n_late - n_early, 2)  # хіба кадр-два "у польоті"

    def test_fresh_push_resumes_after_stale(self):
        fake = FakeSerial()
        disp = SerialDisplay("COMX", heartbeat=0.05, stale_after=0.15,
                             serial_factory=lambda port, baud: fake)
        disp.push([_p("idle", 0)])
        disp.start()
        time.sleep(0.4)  # дані застаріли, відправка стала
        n_stale = len(fake.writes)
        disp.push([_p("printing", 50)])
        time.sleep(0.2)
        disp.stop()
        written = b"".join(fake.writes[n_stale:]).decode("ascii")
        self.assertIn("FW|1|p50\n", written)

    def test_write_failure_reopens_port(self):
        # завислий CDC порт: помилка запису закриває порт, наступний heartbeat
        # відкриває свіжий (після ре-енумерації USB це нове зʼєднання, яке працює)
        fakes = []

        def factory(port, baud):
            f = FakeSerial(fail_writes=(len(fakes) == 0))  # перший порт "завис"
            fakes.append(f)
            return f

        disp = SerialDisplay("COMX", heartbeat=0.02, serial_factory=factory)
        disp.push([_p("printing", 10)])
        disp.start()
        time.sleep(0.5)
        disp.stop()
        self.assertGreaterEqual(len(fakes), 2)     # порт перевідкрився
        self.assertTrue(fakes[0].closed)           # завислий закрито
        written = b"".join(fakes[1].writes).decode("ascii")
        self.assertIn("FW|1|p10\n", written)       # свіже зʼєднання шле кадри

    def test_attach_pushes_current_state_immediately(self):
        # вмикання дисплея у налаштуваннях не має чекати наступного циклу
        # монітора: attach одразу віддає поточний стан
        mon = SimpleNamespace(on_update_complete=None,
                              get_all_printers=lambda: [_p("printing", 42)],
                              _serial_display=None)
        cfg = {"serial": {"enabled": True, "port": "COM255", "baud": 115200}}
        disp = serial_output.attach(mon, cfg)
        try:
            self.assertIsNotNone(disp)
            self.assertEqual(disp._last_frame, "FW|1|p42\n")
        finally:
            if disp is not None:
                disp.stop()


if __name__ == "__main__":
    unittest.main()
