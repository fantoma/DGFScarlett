"""Prova della finestra senza schermo, con la scheda simulata (tests/fake_amixer.py).
Saltata se PySide6 non c'è.   python3 -m unittest discover -s tests"""
import importlib.util
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


@unittest.skipIf(importlib.util.find_spec("PySide6") is None, "PySide6 non installato")
class Gui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ.update(QT_QPA_PLATFORM="offscreen", DGFSCARLETT_MIDI="off",
                          DGFSCARLETT_AMIXER=os.path.join(HERE, "fake_amixer.py"),
                          FAKE_AMIXER_STATE=os.path.join(cls.tmp.name, "state.json"))
        import dgfscarlett as d
        from PySide6.QtWidgets import QApplication
        d.CONFIG = os.path.join(cls.tmp.name, "config.json")
        QApplication.exec = lambda self=None: 0          # costruisce la finestra e torna subito

        class Args:
            card = "USB"
        d.gui(Args())
        cls.d, cls.app = d, QApplication.instance()
        cls.w = next(x for x in cls.app.topLevelWidgets() if x.__class__.__name__ == "Window")

    @classmethod
    def tearDownClass(cls):
        cls.w.close()
        cls.tmp.cleanup()

    def pump(self, t):
        end = time.time() + t
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def test_tabs(self):
        tabs = [self.w.tabs.tabText(i) for i in range(self.w.tabs.count())]
        for t in ("Ingressi", "Mixer", "Matrice mix", "Patch bay", "Uscite", "MIDI"):
            self.assertIn(t, tabs)
        for i in range(self.w.tabs.count()):            # ogni scheda si disegna senza errori
            self.w.tabs.setCurrentIndex(i)
            self.w.grab()

    def test_patchbay(self):
        n = "Analogue Output 03 Playback Enum"
        pb = next(m for m in self.w.matrices if m.__class__.__name__ == "PatchBay")

        class Click:
            def button(self):
                from PySide6.QtCore import Qt
                return Qt.LeftButton
        pb.cell_press(n, "Mix A", Click())
        self.assertEqual(self.w.C(n).text(), "Mix A")
        self.assertEqual(self.w.mix_dests("A"), ["Out 3"])
        pb.cell_press(n, "Mix A", Click())
        self.assertEqual(self.w.C(n).text(), "Off")

    def test_midi_learn_and_mute(self):
        n = "Mix B Input 03 Playback Volume"
        self.w.set_learn(True)
        self.w.learn_pick(n)
        self.w.on_midi("p", "cc", 0, 7, 0.5)
        self.w.set_learn(False)
        self.assertEqual(self.w.midimap[("cc", 0, 7)], n)
        self.w.on_midi("p", "cc", 0, 7, 0.78)
        self.assertEqual(self.w.C(n).text(), "+0.0 dB")
        self.w.toggle_softmute(n)
        self.assertEqual(self.w.C(n).text(), "-∞")
        self.w.toggle_softmute(n)
        self.assertEqual(self.w.C(n).text(), "+0.0 dB")

    def test_meters(self):
        self.assertIsNotNone(self.w.meter_ctl)
        self.w.tabs.setCurrentIndex(0)
        self.w.show()
        self.w._meter_gate()
        self.pump(1.5)
        # con la scheda Ingressi aperta: gli 8 ingressi analogici (instradati ai PCM) si muovono
        self.assertGreaterEqual(sum(1 for m, _ in self.w.meters if m.active and m.isVisible()), 8)
        self.assertTrue(any(m.peak > -60 for m, _ in self.w.meters if m.active))


if __name__ == "__main__":
    unittest.main()
