"""Тести формату кадру і відправника SerialDisplay. Без заліза (fake serial)."""

import time
import unittest
from types import SimpleNamespace

import serial_output
from serial_output import build_frame, SerialDisplay


def _p(status, progress, online=True):
    return SimpleNamespace(status=status, progress=progress, online=online)


class FakeSerial:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, b):
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


if __name__ == "__main__":
    unittest.main()
