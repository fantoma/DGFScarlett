"""Test senza scheda e senza interfaccia:  python3 -m unittest discover -s tests"""
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["DGFSCARLETT_AMIXER"] = os.path.join(HERE, "fake_amixer.py")
os.environ["FAKE_AMIXER_STATE"] = os.path.join(HERE, ".state-test.json")

import dgfscarlett as d  # noqa: E402


class FakeProc:
    def __init__(self, text):
        self.stdout = io.StringIO(text)


class Core(unittest.TestCase):
    def setUp(self):
        try:
            os.remove(os.environ["FAKE_AMIXER_STATE"])
        except OSError:
            pass
        self.card = d.Card("USB")
        self.card.read()

    tearDown = setUp

    def test_parse(self):
        c = self.card.ctls["Mix A Input 01 Playback Volume"]
        self.assertEqual((c.min, c.max, c.db), (0, 172, (-80.0, 0.5)))
        self.assertEqual(c.text(), "+0.0 dB")
        self.assertIn("PCM 3", self.card.ctls["Analogue Output 03 Playback Enum"].items)

    def test_set_and_persist(self):
        n = "Mix C Input 02 Playback Volume"
        c = self.card.ctls[n]
        self.card.set(n, c.parse_value("-12dB"))
        self.assertEqual(d.Card("USB").read()[n].text(), "-12.0 dB")

    def test_taper(self):
        c = self.card.ctls["Mix A Input 01 Playback Volume"]
        self.assertEqual(d.fader_raw(c, 0.0), c.min)
        self.assertEqual(d.fader_raw(c, 1.0), c.max)
        self.assertEqual(c.to_db(d.fader_raw(c, 0.78)), 0.0)
        for raw in range(c.min, c.max + 1):
            self.assertEqual(d.fader_raw(c, d.fader_pos(c, raw)), raw)
        out = self.card.ctls["Line 03 Playback Volume"]          # da -127 a 0 dB
        self.assertEqual(d.fader_raw(out, 1.0), out.max)
        self.assertLess(d.fader_pos(out, out.from_db(-15)), 0.51)

    def test_routes_and_names(self):
        routes = [n for n, c in self.card.ctls.items() if d.is_route(c)]
        self.assertEqual(len(routes), 10 + 2 + 8 + 18 + 18)
        self.assertFalse(d.is_route(self.card.ctls["Clock Source Clock Source"]))
        self.assertEqual(d.dest_label("Analogue Output 03 Playback Enum"), "Out 3")
        self.assertEqual(d.pretty("mute:Mix B Input 03 Playback Volume"), "Mix B · In 3 · mute")
        self.assertEqual(d.pretty("Line 07 (Headphones 1 L) Playback Volume"), "Out 7 (Headphones 1 L)")


    def test_meter_order(self):
        routes = [n for n, c in self.card.ctls.items() if d.is_route(c)]
        new = d.meter_dests(routes, 56)
        old = d.meter_dests(routes, 56, old_first=True)
        self.assertEqual(new[0], "Analogue Output 01 Playback Enum")
        self.assertEqual(new[38], "PCM 01 Capture Enum")
        self.assertEqual(old[0], "PCM 01 Capture Enum")
        self.assertEqual(old[18], "Analogue Output 01 Playback Enum")
        self.assertIsNone(d.meter_dests(routes, 99))


class Midi(unittest.TestCase):
    def setUp(self):
        self.ev = []
        self.mi = d.MidiIn(lambda *a: self.ev.append(a))

    def test_decode_bytes(self):
        self.mi.decode("p", [0xB2, 7, 127])
        self.mi.decode("p", [0x90, 60, 64])
        self.mi.decode("p", [0x80, 60, 0])
        self.mi.decode("p", [0xE0, 0x7F, 0x7F])
        self.assertEqual(self.ev, [("p", "cc", 2, 7, 1.0), ("p", "note", 0, 60, 64 / 127),
                                   ("p", "note", 0, 60, 0.0), ("p", "pb", 0, 0, 1.0)])

    def test_aseqdump_output(self):
        text = ("Waiting for data. Press Ctrl+C to end.\n"
                "Source  Event                  Ch  Data\n"
                " 20:0   Control change          0, controller 7, value 127\n"
                " 20:0   Note on                 1, note 60, velocity 100\n"
                " 20:0   Pitch bend              0, value -8192\n")
        self.mi._seq_reader(FakeProc(text), {"20:0": "nanoKONTROL2: nanoKONTROL2 MIDI 1"})
        self.assertEqual([e[1:4] for e in self.ev], [("cc", 0, 7), ("note", 1, 60), ("pb", 0, 0)])
        self.assertEqual(self.ev[0][4], 1.0)
        self.assertEqual(self.ev[2][4], 0.0)
        self.assertEqual(self.ev[0][0], "nanoKONTROL2: nanoKONTROL2 MIDI 1")


if __name__ == "__main__":
    unittest.main()
