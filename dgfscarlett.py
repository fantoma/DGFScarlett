#!/usr/bin/env python3
"""DGFScarlett — pannello di controllo per le schede Focusrite Scarlett / Clarett su Linux.

Usa i controlli ALSA esposti dal driver «scarlett2» del kernel (gli stessi di Focusrite Control su
Windows/macOS) tramite «amixer» (pacchetto alsa-utils). Pensato per la Scarlett 18i20 2ª gen., funziona con
ogni modello supportato dal driver: le sezioni si costruiscono dai controlli che la scheda espone.

Finestra a schede: Ingressi · Mixer (fader per ogni mix) · Matrice mix · Patch bay · Uscite · MIDI · Altro.
I fader (e i pulsanti) si comandano da un controller USB MIDI con «MIDI learn».

Uso:
  dgfscarlett.py                       finestra grafica
  dgfscarlett.py list [filtro]         elenco dei controlli con il valore attuale
  dgfscarlett.py get "NOME"            valore di un controllo
  dgfscarlett.py set "NOME" VALORE     imposta: numero grezzo, «-12dB», on/off, voce di menu («PCM 3»)
  dgfscarlett.py save FILE.json        salva lo stato della scheda (preset)
  dgfscarlett.py load FILE.json        ripristina un preset
  dgfscarlett.py midi-ports            porte MIDI in ingresso disponibili
Opzione: --card ID   (id ALSA della scheda, es. USB; senza: la prima Focusrite trovata)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

APP = "DGFScarlett"
VERSION = "2.0"
CONFIG = os.path.expanduser("~/.config/dgfscarlett/config.json")


# =============================================================================== ALSA (amixer)
class AlsaError(Exception):
    pass


def _amixer(args, timeout=4.0):
    exe = os.environ.get("DGFSCARLETT_AMIXER") or shutil.which("amixer")
    if not exe:
        raise AlsaError("«amixer» non trovato: installa alsa-utils  (sudo apt install alsa-utils)")
    env = dict(os.environ, LANG="C", LC_ALL="C")
    try:
        r = subprocess.run([exe] + args, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise AlsaError("amixer non risponde")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "errore di amixer").strip().splitlines()
        raise AlsaError(msg[-1] if msg else "errore di amixer")
    return r.stdout


def alsa_cards():
    """[(indice, id, nome breve, nome lungo)]"""
    out = []
    try:
        with open("/proc/asound/cards") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    i = 0
    while i < len(lines):
        m = re.match(r"\s*(\d+)\s+\[(\S+)\s*\]:\s*(.*?)\s+-\s+(.*)$", lines[i])
        if m:
            out.append((int(m.group(1)), m.group(2), m.group(4).strip(),
                        lines[i + 1].strip() if i + 1 < len(lines) else ""))
            i += 2
        else:
            i += 1
    return out


def find_card(wanted=None):
    """(id, nome) della scheda: quella indicata o la prima Focusrite/Scarlett/Clarett."""
    cards = alsa_cards()
    if wanted:
        for idx, cid, short, long in cards:
            if wanted in (cid, str(idx)) or wanted.lower() in short.lower():
                return cid, short
        return wanted, wanted                    # /proc non leggibile: si prova comunque
    for idx, cid, short, long in cards:
        if any(k in (short + long).lower() for k in ("focusrite", "scarlett", "clarett", "vocaster")):
            return cid, short
    raise AlsaError("nessuna scheda Focusrite trovata (collegata e accesa? «aplay -l» per vederle)")


class Control:
    def __init__(self, numid, name):
        self.numid, self.name = numid, name
        self.type, self.count, self.min, self.max = "int", 1, 0, 1
        self.items, self.values, self.db, self.writable = [], [], None, True

    @property
    def value(self):
        return self.values[0] if self.values else None

    def to_db(self, raw):
        if self.db is None or raw is None:
            return None
        if self.db[0] == "minmax":
            lo, hi = self.db[1], self.db[2]
            return lo + (hi - lo) * (raw - self.min) / max(1, self.max - self.min)
        return self.db[0] + (raw - self.min) * self.db[1]

    def from_db(self, db):
        if self.db is None:
            return None
        if self.db[0] == "minmax":
            lo, hi = self.db[1], self.db[2]
            f = (db - lo) / (hi - lo) if hi != lo else 0
            return int(round(self.min + max(0.0, min(1.0, f)) * (self.max - self.min)))
        raw = self.min + (db - self.db[0]) / self.db[1] if self.db[1] else self.min
        return int(max(self.min, min(self.max, round(raw))))

    def text(self, raw=None):
        raw = self.value if raw is None else raw
        if raw is None:
            return "?"
        if self.type == "bool":
            return "on" if raw else "off"
        if self.type == "enum":
            return self.items[raw] if 0 <= raw < len(self.items) else str(raw)
        db = self.to_db(raw)
        if db is not None:
            return "-∞" if raw == self.min and db <= -79.9 else f"{db:+.1f} dB"
        return str(raw)

    def parse_value(self, text):
        """Testo dell'utente → valore grezzo."""
        t = str(text).strip()
        if self.type == "bool":
            if t.lower() in ("1", "on", "true", "si", "sì", "acceso"):
                return 1
            if t.lower() in ("0", "off", "false", "no", "spento"):
                return 0
            raise AlsaError(f"valore non valido per un interruttore: {t}")
        if self.type == "enum":
            for i, it in enumerate(self.items):
                if it.lower() == t.lower():
                    return i
            if t.isdigit() and int(t) < len(self.items):
                return int(t)
            raise AlsaError(f"«{t}» non è tra le voci: {', '.join(self.items)}")
        m = re.fullmatch(r"(-?[\d.]+|-inf)\s*db", t.lower())
        if m:
            if self.db is None:
                raise AlsaError("questo controllo non ha una scala in dB")
            return self.min if m.group(1) == "-inf" else self.from_db(float(m.group(1)))
        v = int(t)
        if not self.min <= v <= self.max:
            raise AlsaError(f"valore fuori intervallo ({self.min}…{self.max})")
        return v


def parse_contents(text):
    ctls, cur = [], None
    for line in text.splitlines():
        m = re.match(r"numid=(\d+),iface=(\w+)(?:,[^']*?)?,name='(.*)'(?:,index=\d+)?(?:,device=\d+)?\s*$", line)
        if m:
            cur = Control(int(m.group(1)), m.group(3)) if m.group(2) == "MIXER" else None
            if cur is not None:
                ctls.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("; type="):
            kv = dict(p.split("=", 1) for p in s[2:].split(",") if "=" in p)
            cur.type = {"INTEGER": "int", "BOOLEAN": "bool", "ENUMERATED": "enum"}.get(kv.get("type"), "other")
            cur.count = int(kv.get("values", 1) or 1)
            cur.writable = "w" in kv.get("access", "rw")[:2]
            if cur.type == "int":
                cur.min, cur.max = int(kv.get("min", 0)), int(kv.get("max", 1))
        elif s.startswith("; Item #"):
            im = re.match(r"; Item #\d+ '(.*)'", s)
            if im:
                cur.items.append(im.group(1))
        elif s.startswith(": values="):
            vals = []
            for r in s[9:].split(","):
                r = r.strip()
                if r in ("on", "off"):
                    vals.append(1 if r == "on" else 0)
                elif re.fullmatch(r"-?\d+", r):
                    vals.append(int(r))
            cur.values = vals
        elif s.startswith("| dBscale-min="):
            dm = re.match(r"\| dBscale-min=(-?[\d.]+)dB,step=(-?[\d.]+)dB", s)
            if dm:
                cur.db = (float(dm.group(1)), float(dm.group(2)))
        elif s.startswith("| dBminmax-min="):
            dm = re.match(r"\| dBminmax-min=(-?[\d.]+)dB,max=(-?[\d.]+)dB", s)
            if dm:
                cur.db = ("minmax", float(dm.group(1)), float(dm.group(2)))
    for c in ctls:
        if c.type == "enum":
            c.min, c.max = 0, max(0, len(c.items) - 1)
        elif c.type == "bool":
            c.min, c.max = 0, 1
    return [c for c in ctls if c.type in ("int", "bool", "enum")]


class Card:
    def __init__(self, card_id):
        self.id = card_id
        self.ctls = {}

    def read(self):
        ctls = parse_contents(_amixer(["-c", self.id, "contents"]))
        self.ctls = {c.name: c for c in ctls}
        return self.ctls

    def set(self, name, raw):
        c = self.ctls.get(name)
        if c is None:
            raise AlsaError(f"controllo «{name}» non trovato")
        if not c.writable:
            m = re.match(r"^Line (\d+)\b", name)
            hint = f"Line Out {int(m.group(1)):02d} Volume Control Playback Enum" if m else ""
            if hint in self.ctls:
                raise AlsaError(f"«{name}» è sulla manopola della scheda (HW): prima  set \"{hint}\" SW")
            raise AlsaError(f"«{name}» è in sola lettura")
        v = ("on" if raw else "off") if c.type == "bool" else str(int(raw))
        _amixer(["-c", self.id, "cset", f"numid={c.numid}", ",".join([v] * max(1, c.count))])
        c.values = [int(raw)] * max(1, c.count)

    def snapshot(self):
        return {n: c.text() for n, c in sorted(self.ctls.items()) if c.writable}


# =============================================================================== nomi
def short_name(name):
    n = re.sub(r"\s+(Playback|Capture)?\s*(Switch|Volume|Enum)$", "", name).strip()
    return re.sub(r"\s+(Playback|Capture)$", "", n).strip() or name


DEST_PATTERNS = ((r"^Analogue Output (\d+)", "Out {}"), (r"^S/PDIF Output (\d+)", "S/PDIF {}"),
                 (r"^ADAT Output (\d+)", "ADAT {}"), (r"^PCM (\d+) Capture", "PCM {}"),
                 (r"^Mixer Input (\d+)", "Mixer In {}"), (r"^DSP Input (\d+)", "DSP {}"))
DEST_GROUPS = (("aout", "Uscite analogiche", r"^Analogue Output"), ("spdif", "S/PDIF", r"^S/PDIF Output"),
               ("adat", "ADAT", r"^ADAT Output"), ("pcm", "Registrazione → computer (PCM)", r"^PCM \d+ Capture"),
               ("mixin", "Ingressi del mixer", r"^Mixer Input"), ("dsp", "DSP", r"^DSP Input"), ("other", "Altro", r"."))
SRC_TITLES = {"Analogue": "Ingressi analogici", "S/PDIF": "S/PDIF", "ADAT": "ADAT", "Mix": "Mix",
              "PCM": "PCM (dal computer)", "DSP": "DSP"}
SRC_RE = re.compile(r"^(Off|(Analogue|PCM|S/PDIF|ADAT|DSP|Mix)\s+\S+)$")


def dest_label(name):
    for pat, fmt in DEST_PATTERNS:
        m = re.match(pat, name)
        if m:
            return fmt.format(int(m.group(1)))
    return short_name(name)


def is_route(ctl):
    """Un menu di instradamento: le voci sono sorgenti (Off, Analogue 1, PCM 3, Mix A…)."""
    if ctl.type != "enum" or "Volume Control" in ctl.name or "Clock" in ctl.name or len(ctl.items) < 4:
        return False
    return sum(1 for it in ctl.items if SRC_RE.match(it)) >= 0.8 * len(ctl.items)


def pretty(target):
    """Nome leggibile di un controllo (o bersaglio MIDI)."""
    if target.startswith("mute:"):
        return pretty(target[5:]) + " · mute"
    m = re.match(r"^Mix ([A-Z]) Input (\d+)", target)
    if m:
        return f"Mix {m.group(1)} · In {int(m.group(2))}"
    m = re.match(r"^Line (\d+)\b(?:\s*\(([^)]*)\))?.*Playback Volume$", target)
    if m:
        return f"Out {int(m.group(1))}" + (f" ({m.group(2)})" if m.group(2) else "")
    m = re.match(r"^Line (\d+) Mute", target)
    if m:
        return f"Out {int(m.group(1))} · mute"
    for pat, fmt in DEST_PATTERNS:
        if re.match(pat, target):
            return dest_label(target)
    return short_name(target)


# =============================================================================== curva dei fader
# posizione del fader 0…1 ↔ dB: 0 dB a circa 3/4 della corsa, come su un mixer vero
_KNOTS = ((0.06, -60.0), (0.25, -35.0), (0.5, -15.0), (0.78, 0.0))


def _taper_pts(ctl):
    lo, hi = ctl.to_db(ctl.min), ctl.to_db(ctl.max)
    return [(0.0, lo)] + [(p, d) for p, d in _KNOTS if lo < d < hi] + [(1.0, hi)]


def fader_pos(ctl, raw):
    if raw is None:
        return 0.0
    if ctl.db is None or ctl.type != "int":
        return (raw - ctl.min) / max(1, ctl.max - ctl.min)
    if raw <= ctl.min:
        return 0.0
    db, pts = ctl.to_db(raw), _taper_pts(ctl)
    for (p0, d0), (p1, d1) in zip(pts, pts[1:]):
        if db <= d1:
            return p0 + (p1 - p0) * ((db - d0) / (d1 - d0) if d1 != d0 else 0)
    return 1.0


def fader_raw(ctl, pos):
    pos = max(0.0, min(1.0, pos))
    if ctl.db is None or ctl.type != "int":
        return int(round(ctl.min + pos * (ctl.max - ctl.min)))
    if pos <= 0:
        return ctl.min
    pts = _taper_pts(ctl)
    for (p0, d0), (p1, d1) in zip(pts, pts[1:]):
        if pos <= p1:
            return ctl.from_db(d0 + (d1 - d0) * ((pos - p0) / (p1 - p0) if p1 != p0 else 0))
    return ctl.max


# =============================================================================== MIDI in ingresso
class MidiIn:
    """Ingresso MIDI: python-rtmidi se c'è, altrimenti «aseqdump» (alsa-utils). Entrambi usano il
    sequencer ALSA, quindi il controller resta usabile anche da altri programmi.
    callback(porta, tipo, canale, numero, valore 0…1) con tipo «cc», «note» o «pb» (pitch bend)."""

    def __init__(self, callback):
        self.cb = callback
        self.backend = None
        self.opened = []
        self._rt = []
        self._proc = None
        try:
            import rtmidi
            if hasattr(rtmidi, "MidiIn"):
                self.rtmidi, self.backend = rtmidi, "python-rtmidi"
        except Exception:
            pass
        if not self.backend and shutil.which("aseqdump"):
            self.backend = "aseqdump"

    @staticmethod
    def _skip(name):
        return any(k in name for k in ("RtMidi", "Midi Through", "aseqdump", APP))

    def _seq_ports(self):
        try:
            out = subprocess.run(["aseqdump", "-l"], capture_output=True, text=True, timeout=3,
                                 env=dict(os.environ, LANG="C", LC_ALL="C")).stdout
        except (OSError, subprocess.TimeoutExpired):
            return {}
        res = {}
        for line in out.splitlines():
            m = re.match(r"^\s*(\d+):(\d+)\s+(.+?)\s{2,}(.+?)\s*$", line)
            if m and m.group(1) != "0":
                name = f"{m.group(3)}: {m.group(4)}"
                if not self._skip(name):
                    res[name] = f"{m.group(1)}:{m.group(2)}"
        return res

    def _rt_names(self):
        try:
            return [re.sub(r"\s+\d+:\d+$", "", n) for n in self.rtmidi.MidiIn().get_ports()]
        except Exception:
            return []

    def ports(self):
        if self.backend == "python-rtmidi":
            return [n for n in self._rt_names() if not self._skip(n)]
        if self.backend == "aseqdump":
            return list(self._seq_ports())
        return []

    def close(self):
        for m in self._rt:
            try:
                m.cancel_callback()
                m.close_port()
            except Exception:
                pass
        self._rt = []
        if self._proc:
            try:
                self._proc.terminate()
            except OSError:
                pass
            self._proc = None
        self.opened = []

    def open(self, names):
        self.close()
        names = set(names)
        if not names or not self.backend:
            return []
        if self.backend == "python-rtmidi":
            for i, n in enumerate(self._rt_names()):
                if n in names and n not in self.opened:
                    try:
                        m = self.rtmidi.MidiIn()
                        m.open_port(i)
                        m.ignore_types(sysex=True, timing=True, active_sense=True)
                        m.set_callback(self._rt_cb, n)
                        self._rt.append(m)
                        self.opened.append(n)
                    except Exception:
                        pass
        else:
            addrs = {a: n for n, a in self._seq_ports().items() if n in names}
            if addrs:
                cmd = ["aseqdump", "-p", ",".join(addrs)]
                if shutil.which("stdbuf"):
                    cmd = ["stdbuf", "-oL"] + cmd
                try:
                    self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                                  bufsize=1, env=dict(os.environ, LANG="C", LC_ALL="C"))
                    threading.Thread(target=self._seq_reader, args=(self._proc, addrs), daemon=True).start()
                    self.opened = list(addrs.values())
                except OSError:
                    pass
        return self.opened

    def _rt_cb(self, event, port):
        self.decode(port, event[0])

    def decode(self, port, msg):
        if len(msg) < 2:
            return
        st, ch = msg[0] & 0xF0, msg[0] & 0x0F
        if st == 0xB0 and len(msg) >= 3:
            self.cb(port, "cc", ch, msg[1], msg[2] / 127)
        elif st == 0x90 and len(msg) >= 3:
            self.cb(port, "note", ch, msg[1], msg[2] / 127)
        elif st == 0x80:
            self.cb(port, "note", ch, msg[1], 0.0)
        elif st == 0xE0 and len(msg) >= 3:
            self.cb(port, "pb", ch, 0, ((msg[2] << 7) | msg[1]) / 16383)

    def _seq_reader(self, proc, addrs):
        rx = re.compile(r"^\s*(\d+:\d+)\s+(Control change|Note on|Note off|Pitch bend)\s+(\d+),\s*(.*)$")
        for line in proc.stdout:
            m = rx.match(line)
            if not m:
                continue
            port, ev, ch = addrs.get(m.group(1), m.group(1)), m.group(2), int(m.group(3))
            nums = [int(x) for x in re.findall(r"-?\d+", m.group(4))]
            try:
                if ev == "Control change":
                    self.cb(port, "cc", ch, nums[0], nums[1] / 127)
                elif ev == "Note on":
                    self.cb(port, "note", ch, nums[0], nums[1] / 127)
                elif ev == "Note off":
                    self.cb(port, "note", ch, nums[0], 0.0)
                elif ev == "Pitch bend":
                    self.cb(port, "pb", ch, 0, (nums[0] + 8192) / 16383)
            except (IndexError, RuntimeError):
                pass


def midi_text(kind, ch, num):
    k = {"cc": f"CC {num}", "note": f"Nota {num}", "pb": "Pitch bend"}.get(kind, kind)
    return f"{k} · ch {ch + 1}"


# =============================================================================== configurazione
def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        tmp = CONFIG + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=1, ensure_ascii=False)
        os.replace(tmp, CONFIG)
    except OSError:
        pass


# =============================================================================== riga di comando
def cli(args):
    if args.cmd == "midi-ports":
        mi = MidiIn(lambda *a: None)
        print(f"Ingresso MIDI: {mi.backend or 'non disponibile (installa alsa-utils o python-rtmidi)'}")
        for p in mi.ports():
            print("  ", p)
        return 0
    cid, cname = find_card(args.card)
    card = Card(cid)
    card.read()
    if args.cmd == "list":
        f = (args.args[0] if args.args else "").lower()
        for n, c in sorted(card.ctls.items()):
            if f in n.lower():
                ro = "" if c.writable else "  (sola lettura)"
                extra = f"  [{', '.join(c.items)}]" if c.type == "enum" and len(c.items) <= 8 else ""
                print(f"{n}: {c.text()}{ro}{extra}")
        return 0
    if args.cmd == "get":
        c = card.ctls.get(args.args[0])
        if c is None:
            raise AlsaError(f"controllo «{args.args[0]}» non trovato (usa «list»)")
        print(c.text())
        return 0
    if args.cmd == "set":
        name, val = args.args[0], args.args[1]
        c = card.ctls.get(name)
        if c is None:
            raise AlsaError(f"controllo «{name}» non trovato (usa «list»)")
        card.set(name, c.parse_value(val))
        print(f"{name} = {c.text()}")
        return 0
    if args.cmd == "save":
        data = {"scheda": cname, "salvato": time.strftime("%Y-%m-%d %H:%M:%S"), "controlli": card.snapshot()}
        with open(args.args[0], "w") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        print(f"Salvati {len(data['controlli'])} controlli in {args.args[0]}")
        return 0
    if args.cmd == "load":
        with open(args.args[0]) as f:
            n, errs = apply_preset(card, json.load(f))
        print(f"Ripristinati {n} controlli" + (f"; non applicati: {len(errs)}" if errs else ""))
        for e in errs[:20]:
            print("  ", e)
        return 0
    return 1


def apply_preset(card, data, order_first=("Volume Control",)):
    """Applica un preset (testi come da «save»): prima i modi HW/SW, poi il resto; solo i valori diversi."""
    vals = data.get("controlli", data)
    names = sorted(vals, key=lambda n: (0 if any(k in n for k in order_first) else 1, n))
    done, errs = 0, []
    for name in names:
        c = card.ctls.get(name)
        if c is None:
            errs.append(f"{name}: non presente su questa scheda")
            continue
        try:
            raw = c.parse_value(vals[name].replace(" dB", "dB").replace("-∞", "-infdB").replace("+", ""))
            if raw != c.value:
                card.set(name, raw)
                if any(k in name for k in order_first):
                    card.read()                    # le uscite passate a SW diventano scrivibili
                done += 1
        except AlsaError as e:
            errs.append(f"{name}: {e}")
    return done, errs


# =============================================================================== interfaccia grafica
def gui(args):
    from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
    from PySide6.QtGui import QColor, QFontMetrics, QKeySequence, QPainter, QPalette, QPen, QPolygon, QShortcut
    from PySide6.QtWidgets import (QAbstractItemView, QAbstractScrollArea, QApplication, QCheckBox, QComboBox,
                                   QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                                   QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
                                   QScrollArea, QSlider, QStyle, QTableWidget, QTableWidgetItem, QTabWidget,
                                   QToolTip, QVBoxLayout, QWidget)

    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setStyle("Fusion")
    pal = QPalette()
    for role, rgb in {QPalette.Window: (34, 35, 41), QPalette.WindowText: (225, 225, 230),
                      QPalette.Base: (26, 27, 32), QPalette.AlternateBase: (40, 41, 48),
                      QPalette.Text: (225, 225, 230), QPalette.Button: (52, 54, 62),
                      QPalette.ButtonText: (230, 230, 235), QPalette.Highlight: (196, 30, 58),
                      QPalette.HighlightedText: (255, 255, 255), QPalette.ToolTipBase: (50, 50, 58),
                      QPalette.ToolTipText: (235, 235, 240)}.items():
        pal.setColor(role, QColor(*rgb))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(110, 110, 118))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(110, 110, 118))
    app.setPalette(pal)

    RED = "#c41e3a"
    YELLOW = "#ffcc00"
    TOGGLE = ("QPushButton{background:#3a3c46;color:#bbb;border-radius:4px;padding:4px 8px;}"
              f"QPushButton:checked{{background:{RED};color:white;font-weight:bold;}}")
    TOGGLE_GREEN = ("QPushButton{background:#3a3c46;color:#bbb;border-radius:4px;padding:4px 8px;}"
                    "QPushButton:checked{background:#2e7d46;color:white;font-weight:bold;}")
    LEARN_BTN = {None: "", "armed": "QPushButton{border:1px dashed #d9a400;}",
                 "target": f"QPushButton{{border:2px solid {YELLOW};}}"}
    TINTS = [QColor(70, 40, 52), QColor(40, 54, 76), QColor(40, 68, 50), QColor(74, 66, 38),
             QColor(60, 44, 78), QColor(38, 70, 72), QColor(70, 52, 40)]
    GH, GCW = 22, 26                      # altezza delle righe di gruppo, larghezza di un gruppo chiuso
    GREY = QColor(150, 150, 160)

    class Bridge(QObject):
        loaded = Signal(object)          # dict nome → Control (dal thread di lettura)
        failed = Signal(str)
        midi = Signal(str, str, int, int, float)

    class Worker:
        """Scritture in coda su un thread: l'interfaccia non si blocca, conta solo l'ultimo valore."""

        def __init__(self, card, bridge):
            self.card, self.bridge = card, bridge
            self.pending = {}
            self.lock = threading.Lock()
            self.ev = threading.Event()
            self.busy = threading.Lock()
            threading.Thread(target=self._run, daemon=True).start()

        def set(self, name, raw):
            with self.lock:
                self.pending[name] = raw
            self.ev.set()

        def pending_names(self):
            with self.lock:
                return set(self.pending)

        def _run(self):
            while True:
                self.ev.wait()
                self.ev.clear()
                while True:
                    with self.lock:
                        if not self.pending:
                            break
                        name = next(iter(self.pending))
                        raw = self.pending.pop(name)
                    try:
                        with self.busy:
                            self.card.set(name, raw)
                    except AlsaError as e:
                        self.bridge.failed.emit(f"{pretty(name)}: {e}")

        def read_async(self):
            def job():
                try:
                    with self.busy:
                        ctls = parse_contents(_amixer(["-c", self.card.id, "contents"]))
                    self.bridge.loaded.emit({c.name: c for c in ctls})
                except AlsaError as e:
                    self.bridge.failed.emit(str(e))
            threading.Thread(target=job, daemon=True).start()

    try:
        cid, cname = find_card(args.card)
        card = Card(cid)
        card.read()
    except AlsaError as e:
        QMessageBox.critical(None, APP, str(e))
        return 1

    # ------------------------------------------------------------------ fader
    class Scale(QWidget):
        """Scala in dB accanto al fader."""
        MARKS = (6, 0, -6, -12, -20, -30, -40, -60)

        def __init__(self, slider, ctl):
            super().__init__()
            self.sl, self.ctl = slider, ctl
            self.setFixedWidth(24)

        def paintEvent(self, e):
            c = self.ctl
            if c is None or c.db is None:
                return
            p = QPainter(self)
            f = p.font()
            f.setPixelSize(9)
            p.setFont(f)
            p.setPen(QColor(130, 130, 140))
            hl = self.sl.style().pixelMetric(QStyle.PM_SliderLength, None, self.sl)
            top = self.sl.geometry().top() - self.geometry().top()
            span = self.sl.height() - hl
            lo, hi = c.to_db(c.min), c.to_db(c.max)
            for d in self.MARKS + (None,):
                if d is not None and not lo < d <= hi:
                    continue
                pos = 0.0 if d is None else fader_pos(c, c.from_db(d))
                y = int(top + hl / 2 + (1 - pos) * span)
                txt = "-∞" if d is None else (f"+{d}" if d > 0 else str(d))
                p.drawText(QRect(0, y - 6, 19, 12), Qt.AlignRight | Qt.AlignVCenter, txt)
                p.drawLine(20, y, 23, y)

    class Fader(QFrame):
        """Fader verticale con curva da mixer (0 dB a 3/4), scala in dB e indicazione MIDI."""
        moved = Signal(int)

        def __init__(self, ctl, title, sub="", width=80):
            super().__init__()
            self.setObjectName("fader")
            self.ctl, self.on_pick, self._raw = ctl, None, None
            v = QVBoxLayout(self)
            v.setContentsMargins(3, 3, 3, 3)
            v.setSpacing(3)
            self.title = QLabel(title)
            self.title.setAlignment(Qt.AlignCenter)
            self.title.setStyleSheet("font-weight:bold;")
            self.title.setFixedWidth(width)
            v.addWidget(self.title)
            self.sub = QLabel(sub)
            self.sub.setAlignment(Qt.AlignCenter)
            self.sub.setWordWrap(True)
            self.sub.setStyleSheet("color:#9a9aa5;font-size:11px;")
            self.sub.setFixedWidth(width)
            self.sub.setMinimumHeight(28)
            v.addWidget(self.sub)
            self.val = QLabel()
            self.val.setAlignment(Qt.AlignCenter)
            self.val.setStyleSheet("font-family:monospace;background:#1a1b20;border-radius:3px;padding:1px;")
            self.val.setToolTip("Doppio clic: 0 dB")
            v.addWidget(self.val)
            self.sl = QSlider(Qt.Vertical)
            self.sl.setRange(0, 1000)
            self.sl.setPageStep(50)
            self.sl.setMinimumHeight(200)
            self.sl.installEventFilter(self)
            self.scale = Scale(self.sl, ctl)
            row = QHBoxLayout()
            row.setSpacing(2)
            row.addStretch(1)
            row.addWidget(self.scale)
            row.addWidget(self.sl)
            row.addSpacing(24)
            row.addStretch(1)
            v.addLayout(row, 1)
            self.midi = QLabel("")
            self.midi.setAlignment(Qt.AlignCenter)
            self.midi.setStyleSheet(f"color:{YELLOW};font-size:10px;")
            v.addWidget(self.midi)
            self.extra = QVBoxLayout()
            v.addLayout(self.extra)
            self._set_pos(ctl.value)
            self.sl.valueChanged.connect(self._changed)
            self.val.mouseDoubleClickEvent = lambda e: self.set_db(0.0)
            self.setContextMenuPolicy(Qt.CustomContextMenu)

        def _set_pos(self, raw):
            self.sl.blockSignals(True)
            self.sl.setValue(int(round(fader_pos(self.ctl, raw) * 1000)))
            self.sl.blockSignals(False)
            self._raw = raw
            self.val.setText(self.ctl.text(raw))

        def _changed(self, pos):
            raw = fader_raw(self.ctl, pos / 1000)
            if raw != self._raw:
                self._raw = raw
                self.val.setText(self.ctl.text(raw))
                self.moved.emit(raw)

        def set_db(self, db):
            raw = self.ctl.from_db(db)
            if raw is not None and self.sl.isEnabled():
                self._set_pos(raw)
                self.moved.emit(raw)

        def update_from(self, ctl):
            self.ctl = self.scale.ctl = ctl
            self.sl.setEnabled(ctl.writable)
            if not self.sl.isSliderDown() and ctl.value is not None and ctl.value != self._raw:
                self._set_pos(ctl.value)

        def set_learn(self, state, label):
            self.setStyleSheet({None: "",
                                "armed": "QFrame#fader{border:1px dashed #d9a400;border-radius:6px;}",
                                "target": f"QFrame#fader{{border:2px solid {YELLOW};border-radius:6px;"
                                          "background:#3a3420;}"}[state])
            self.midi.setText(label or "")

        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton and self.on_pick \
                    and self.on_pick():
                return True
            return False

        def mousePressEvent(self, e):
            if e.button() == Qt.LeftButton and self.on_pick and self.on_pick():
                return
            super().mousePressEvent(e)

    # ------------------------------------------------------------------ matrice generica
    class MatrixView(QAbstractScrollArea):
        """Griglia disegnata a mano con intestazioni fisse. Gruppi di righe e di colonne richiudibili
        (clic sul titolo del gruppo); con «expandable» ogni riga si espande cliccandone il nome."""
        expandable = False
        rotate_cols = False
        collapsible_cols = True

        def __init__(self, cw, rh, erh=None, hdr_w=210, hdr_h=GH + 28):
            super().__init__()
            self.cw, self.rh, self.erh = cw, rh, erh or rh
            self.hdr_w, self.hdr_h = hdr_w, hdr_h
            self.row_groups, self.col_groups = [], []
            self.collapsed_rows, self.collapsed_cols, self.expanded = set(), set(), set()
            self.rlay, self.clay, self.cspans = [], [], []
            self.total_w = self.total_h = 0
            self.hover = (None, None)
            self.drag = None
            self.on_layout = None
            self.setFrameShape(QFrame.NoFrame)
            self.viewport().setMouseTracking(True)

        # --- modello e geometria
        def set_model(self, row_groups, col_groups):
            self.row_groups, self.col_groups = row_groups, col_groups
            self.relayout()

        def relayout(self, notify=False):
            self.rlay, y = [], 0
            for gi, (gid, title, keys) in enumerate(self.row_groups):
                if title is not None:
                    self.rlay.append(("g", gid, y, GH, gi))
                    y += GH
                    if gid in self.collapsed_rows:
                        continue
                for k in keys:
                    h = self.erh if k in self.expanded else self.rh
                    self.rlay.append(("r", k, y, h, gi))
                    y += h
            self.clay, self.cspans, x = [], [], 0
            for gi, (gid, title, keys) in enumerate(self.col_groups):
                x0 = x
                if gid in self.collapsed_cols:
                    self.clay.append(("g", gid, x, GCW, gi))
                    x += GCW
                else:
                    for k in keys:
                        self.clay.append(("c", k, x, self.cw, gi))
                        x += self.cw
                self.cspans.append((gid, title, x0, x, gi))
                x += 4
            self.total_w, self.total_h = x, y
            self._bars()
            if notify and self.on_layout:
                self.on_layout()

        def _bars(self):
            vw = self.viewport().width() - self.hdr_w
            vh = self.viewport().height() - self.hdr_h
            for sb, tot, page, step in ((self.horizontalScrollBar(), self.total_w, vw, self.cw),
                                        (self.verticalScrollBar(), self.total_h, vh, self.rh)):
                sb.setRange(0, max(0, tot - page))
                sb.setPageStep(max(1, page))
                sb.setSingleStep(step)
            self.viewport().update()

        def sizeHint(self):
            return QSize(min(1400, self.hdr_w + self.total_w + 20), min(900, self.hdr_h + self.total_h + 20))

        def resizeEvent(self, e):
            super().resizeEvent(e)
            self._bars()

        def scrollContentsBy(self, dx, dy):
            self.viewport().update()

        def set_all_expanded(self, on):
            keys = [k for _, _, ks in self.row_groups for k in ks]
            self.expanded = set(keys) if on else set()
            self.relayout(True)

        def set_all_groups(self, open_):
            self.collapsed_rows = set() if open_ else {g for g, t, _ in self.row_groups if t is not None}
            if self.collapsible_cols:
                self.collapsed_cols = set() if open_ else {g for g, _, _ in self.col_groups}
            self.relayout(True)

        # --- da ridefinire
        def row_label(self, key):
            return str(key), ""

        def col_text(self, key):
            return str(key)

        def col_group_text(self, gid, title):
            return title

        def paint_cell(self, p, r, rkey, ck, ckey, here):
            pass

        def paint_group_row_cell(self, p, r, gid, ck, ckey):
            pass

        def cell_press(self, rkey, ckey, e):
            pass

        def cell_drag(self, e):
            pass

        def cell_double(self, rkey, ckey):
            pass

        def cell_wheel(self, rkey, ckey, e):
            return False

        def cell_menu(self, rkey, ckey, gpos):
            pass

        def cell_tip(self, rkey, ckey):
            return ""

        def row_tip(self, rkey):
            return ""

        # --- disegno
        def paintEvent(self, e):
            p = QPainter(self.viewport())
            f = p.font()
            f.setPixelSize(12)
            p.setFont(f)
            fm = QFontMetrics(f)
            vp = self.viewport().rect()
            W, H = vp.width(), vp.height()
            ox, oy = self.horizontalScrollBar().value(), self.verticalScrollBar().value()
            HW, HH = self.hdr_w, self.hdr_h
            hr, hc = self.hover
            p.fillRect(vp, QColor(22, 23, 27))
            # celle
            p.setClipRect(QRect(HW, HH, W - HW, H - HH))
            for ri, (rk, rkey, y, h, rgi) in enumerate(self.rlay):
                sy = HH + y - oy
                if sy + h < HH or sy > H:
                    continue
                if rk == "g":
                    p.fillRect(QRect(HW, sy, W - HW, h - 1), QColor(44, 45, 54))
                for ci, (ck, ckey, x, w, cgi) in enumerate(self.clay):
                    sx = HW + x - ox
                    if sx + w < HW or sx > W:
                        continue
                    r = QRect(sx, sy, w - 1, h - 1)
                    if rk == "g":
                        self.paint_group_row_cell(p, r, rkey, ck, ckey)
                        continue
                    base = QColor(30, 31, 37) if cgi % 2 == 0 else QColor(34, 35, 42)
                    if ri == hr or ci == hc:
                        base = base.lighter(135)
                    p.fillRect(r, base)
                    self.paint_cell(p, r, rkey, ck, ckey, ri == hr and ci == hc)
            # intestazioni delle righe
            p.setClipRect(QRect(0, HH, HW, H - HH))
            for ri, (rk, rkey, y, h, rgi) in enumerate(self.rlay):
                sy = HH + y - oy
                if sy + h < HH or sy > H:
                    continue
                r = QRect(0, sy, HW - 2, h - 1)
                if rk == "g":
                    p.fillRect(r, QColor(44, 45, 54))
                    p.fillRect(QRect(0, sy, 4, h - 1), TINTS[rgi % len(TINTS)].lighter(160))
                    gid, title, keys = self.row_groups[rgi]
                    arrow = "▸" if gid in self.collapsed_rows else "▾"
                    p.setPen(QColor(230, 230, 235))
                    p.drawText(r.adjusted(10, 0, 0, 0), Qt.AlignLeft | Qt.AlignVCenter,
                               fm.elidedText(f"{arrow}  {title}  ({len(keys)})", Qt.ElideRight, HW - 16))
                    continue
                p.fillRect(r, QColor(52, 53, 63) if ri == hr else QColor(34, 35, 41))
                x = 8
                if self.expandable:
                    p.setPen(GREY)
                    p.drawText(QRect(x, sy, 12, self.rh), Qt.AlignCenter, "▾" if rkey in self.expanded else "▸")
                    x += 16
                main, sub = self.row_label(rkey)
                rr = QRect(x, sy, HW - x - 8, self.rh)
                p.setPen(QColor(225, 225, 230))
                p.drawText(rr, Qt.AlignLeft | Qt.AlignVCenter, main)
                if sub:
                    room = rr.width() - fm.horizontalAdvance(main) - 10
                    if room > 20:
                        p.setPen(GREY)
                        p.drawText(rr, Qt.AlignRight | Qt.AlignVCenter, fm.elidedText(sub, Qt.ElideRight, room))
            # intestazioni delle colonne
            p.setClipRect(QRect(HW, 0, W - HW, HH))
            p.fillRect(QRect(HW, 0, W - HW, HH), QColor(34, 35, 41))
            for gid, title, x0, x1, gi in self.cspans:
                r = QRect(HW + x0 - ox, 0, x1 - x0 - 1, GH - 2)
                p.fillRect(r, TINTS[gi % len(TINTS)])
                p.setPen(QColor(235, 235, 240))
                txt = self.col_group_text(gid, title)
                if self.collapsible_cols:
                    txt = ("▸ " if gid in self.collapsed_cols else "▾ ") + txt
                p.drawText(r.adjusted(4, 0, -2, 0), Qt.AlignLeft | Qt.AlignVCenter,
                           fm.elidedText(txt, Qt.ElideRight, max(0, r.width() - 6)))
            for ci, (ck, ckey, x, w, gi) in enumerate(self.clay):
                r = QRect(HW + x - ox, GH, w - 1, HH - GH - 2)
                if ci == hc:
                    p.fillRect(r, QColor(52, 53, 63))
                p.setPen(QColor(215, 215, 222) if ck == "c" else GREY)
                txt = self.col_text(ckey) if ck == "c" else self.col_groups[gi][1]
                if self.rotate_cols or ck == "g":
                    p.save()
                    p.translate(r.left(), r.bottom())
                    p.rotate(-90)
                    p.drawText(QRect(4, 0, r.height() - 6, r.width()), Qt.AlignLeft | Qt.AlignVCenter,
                               fm.elidedText(txt, Qt.ElideRight, r.height() - 6))
                    p.restore()
                else:
                    p.drawText(r, Qt.AlignCenter, txt)
            p.setClipping(False)
            p.fillRect(QRect(0, 0, HW, HH), QColor(34, 35, 41))
            p.setPen(QPen(QColor(60, 61, 70)))
            p.drawLine(0, HH - 1, W, HH - 1)
            p.drawLine(HW - 1, 0, HW - 1, H)

        # --- interazione
        def hit(self, pos):
            ox, oy = self.horizontalScrollBar().value(), self.verticalScrollBar().value()
            x, y = pos.x(), pos.y()
            ri = ci = band = None
            if y >= self.hdr_h:
                yy = y - self.hdr_h + oy
                ri = next((i for i, it in enumerate(self.rlay) if it[2] <= yy < it[2] + it[3]), None)
            if x >= self.hdr_w:
                xx = x - self.hdr_w + ox
                ci = next((i for i, it in enumerate(self.clay) if it[2] <= xx < it[2] + it[3]), None)
                if y < GH:
                    band = next((s[0] for s in self.cspans if s[2] <= xx < s[3]), None)
            zone = ("cell" if x >= self.hdr_w and y >= self.hdr_h else "row" if y >= self.hdr_h
                    else "col" if x >= self.hdr_w else "corner")
            return zone, ri, ci, band

        def _cell(self, pos):
            zone, ri, ci, _ = self.hit(pos)
            if zone == "cell" and ri is not None and ci is not None:
                return self.rlay[ri], self.clay[ci]
            return None, None

        def mousePressEvent(self, e):
            if e.button() != Qt.LeftButton:
                return
            zone, ri, ci, band = self.hit(e.position().toPoint())
            if zone == "row" and ri is not None:
                rk, key = self.rlay[ri][:2]
                if rk == "g":
                    self.collapsed_rows ^= {key}
                    self.relayout(True)
                elif self.expandable:
                    self.expanded ^= {key}
                    self.relayout(True)
            elif zone == "col" and self.collapsible_cols:
                gid = band if band is not None else (self.clay[ci][1] if ci is not None and
                                                     self.clay[ci][0] == "g" else None)
                if gid is not None:
                    self.collapsed_cols ^= {gid}
                    self.relayout(True)
            elif zone == "cell" and ri is not None and ci is not None:
                (rk, rkey, *_), (ck, ckey, *_) = self.rlay[ri], self.clay[ci]
                if rk == "g":
                    self.collapsed_rows.discard(rkey)
                    self.relayout(True)
                elif ck == "g":
                    self.collapsed_cols.discard(ckey)
                    self.relayout(True)
                else:
                    self.cell_press(rkey, ckey, e)

        def mouseMoveEvent(self, e):
            zone, ri, ci, _ = self.hit(e.position().toPoint())
            hv = (ri if zone in ("cell", "row") else None, ci if zone in ("cell", "col") else None)
            if hv != self.hover:
                self.hover = hv
                self.viewport().update()
            if self.drag is not None:
                self.cell_drag(e)

        def mouseReleaseEvent(self, e):
            self.drag = None

        def leaveEvent(self, e):
            self.hover = (None, None)
            self.viewport().update()

        def mouseDoubleClickEvent(self, e):
            r, c = self._cell(e.position().toPoint())
            if r and r[0] == "r" and c[0] == "c":
                self.cell_double(r[1], c[1])

        def wheelEvent(self, e):
            r, c = self._cell(e.position().toPoint())
            if r and r[0] == "r" and c[0] == "c" and self.cell_wheel(r[1], c[1], e):
                e.accept()
                return
            super().wheelEvent(e)

        def contextMenuEvent(self, e):
            r, c = self._cell(e.pos())
            if r and r[0] == "r" and c[0] == "c":
                self.cell_menu(r[1], c[1], e.globalPos())

        def viewportEvent(self, e):
            if e.type() == QEvent.ToolTip:
                zone, ri, ci, _ = self.hit(e.pos())
                tip = ""
                if zone == "cell" and ri is not None and ci is not None and self.rlay[ri][0] == "r" \
                        and self.clay[ci][0] == "c":
                    tip = self.cell_tip(self.rlay[ri][1], self.clay[ci][1])
                elif zone == "row" and ri is not None:
                    tip = (self.row_tip(self.rlay[ri][1]) if self.rlay[ri][0] == "r"
                           else "Clic: apri / chiudi il gruppo")
                elif zone == "col" and self.collapsible_cols:
                    tip = "Clic sul gruppo: apri / chiudi"
                if tip:
                    QToolTip.showText(e.globalPos(), tip, self.viewport())
                else:
                    QToolTip.hideText()
                return True
            return super().viewportEvent(e)

    # ------------------------------------------------------------------ patch bay
    class PatchBay(MatrixView):
        """Righe = destinazioni (uscite, PCM verso il computer, ingressi del mixer); colonne = sorgenti."""

        def __init__(self, win, routes):
            super().__init__(cw=22, rh=21, hdr_w=250, hdr_h=GH + 26)
            self.win = win
            rows, done = [], set()
            for gid, title, pat in DEST_GROUPS:
                names = [n for n in routes if re.search(pat, n) and n not in done]
                names.sort(key=lambda n: (int(m.group(1)) if (m := re.search(r"(\d+)", n)) else 0, n))
                if names:
                    rows.append((gid, title, names))
                    done.update(names)
            items = []
            for n in sorted(routes, key=lambda n: -len(card.ctls[n].items)):
                for it in card.ctls[n].items:
                    if it != "Off" and it not in items:
                        items.append(it)
            cols, order = {}, []
            for it in items:
                m = re.match(r"^(.*?)\s+(\S+)$", it)
                g = m.group(1) if m else it
                if g not in cols:
                    cols[g] = []
                    order.append(g)
                cols[g].append(it)
            self.members = {g: set(v) for g, v in cols.items()}
            self.set_model(rows, [(g, SRC_TITLES.get(g, g), cols[g]) for g in order])

        def row_label(self, name):
            c = card.ctls.get(name)
            main = dest_label(name)
            lab = self.win.out_labels.get(main)
            if lab:
                main += f"  {lab}"
            return main, (c.text() if c else "")

        def col_text(self, item):
            return item.rsplit(" ", 1)[-1]

        def row_tip(self, name):
            return f"{name}\nSorgente: {card.ctls[name].text()}"

        def paint_cell(self, p, r, name, ck, item, here):
            c = card.ctls.get(name)
            if c is None:
                return
            cur = c.text()
            if ck == "g":
                if cur in self.members.get(item, ()):
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(170, 90, 100))
                    p.drawEllipse(r.center(), 4, 4)
                return
            if item not in c.items:
                p.fillRect(r, QColor(22, 23, 27))
                return
            if cur == item:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(RED))
                p.drawEllipse(r.center(), 6, 6)
            elif here:
                p.setPen(QPen(QColor(150, 150, 160)))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(r.center(), 5, 5)

        def paint_group_row_cell(self, p, r, gid, ck, ckey):
            if gid not in self.collapsed_rows or ck != "c":
                return
            names = next(ks for g, _, ks in self.row_groups if g == gid)
            if any(card.ctls[n].text() == ckey for n in names if n in card.ctls):
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(170, 90, 100))
                p.drawEllipse(r.center(), 4, 4)

        def cell_press(self, name, item, e):
            c = card.ctls.get(name)
            if c is None or item not in c.items:
                return
            if c.text() == item:
                if "Off" not in c.items:
                    return
                raw = c.items.index("Off")
            else:
                raw = c.items.index(item)
            self.win.apply(name, raw)

        def cell_menu(self, name, item, gpos):
            m = QMenu(self)
            c = card.ctls.get(name)
            if c and "Off" in c.items:
                m.addAction(f"Scollega {dest_label(name)} (Off)", lambda: self.win.apply(name, c.items.index("Off")))
            if not m.isEmpty():
                m.exec(gpos)

        def cell_tip(self, name, item):
            c = card.ctls.get(name)
            on = c is not None and c.text() == item
            return f"{item} → {dest_label(name)}" + ("   ● collegato (clic: scollega)" if on else "   (clic: collega)")

    # ------------------------------------------------------------------ matrice dei mix
    class GainMatrix(MatrixView):
        """Righe = ingressi del mixer (con la loro sorgente); colonne = mix; celle = livello.
        Trascina in verticale o usa la rotella; doppio clic 0 dB ↔ -∞; clic sul nome: espandi la riga."""
        expandable = True
        rotate_cols = True
        collapsible_cols = False

        def __init__(self, win, inputs, mixes, names):
            super().__init__(cw=46, rh=24, erh=78, hdr_w=230, hdr_h=GH + 110)
            self.win, self.names = win, names
            self.set_model([("in", None, inputs)], [("mix", "Mix", mixes)])

        def row_label(self, i):
            s = card.ctls.get(f"Mixer Input {i:02d} Capture Enum")
            return f"In {i}", (s.text() if s else "")

        def col_text(self, x):
            d = self.win.mix_dests(x)
            return f"Mix {x}" + (f"  → {', '.join(d)}" if d else "")

        def ctl(self, i, x):
            n = self.names.get((i, x))
            return n, (card.ctls.get(n) if n else None)

        def paint_cell(self, p, r, i, ck, x, here):
            n, c = self.ctl(i, x)
            if c is None:
                p.fillRect(r, QColor(22, 23, 27))
                return
            pos = fader_pos(c, c.value)
            muted = n in self.win.softmute
            db = c.to_db(c.value)
            txt = "" if pos <= 0 or db is None else (f"{db:+.0f}" if round(db) else "0")
            if r.height() > self.rh:                                   # riga espansa: barra verticale
                bar = QRect(r.center().x() - 6, r.top() + 18, 12, r.height() - 24)
                p.fillRect(bar, QColor(18, 18, 22))
                hfill = int(bar.height() * pos)
                p.fillRect(QRect(bar.left(), bar.bottom() - hfill + 1, bar.width(), hfill),
                           QColor(110, 110, 120) if muted else QColor(RED))
                p0 = fader_pos(c, c.from_db(0.0)) if c.db else None
                if p0 is not None and 0 < p0 < 1:
                    y0 = bar.bottom() - int(bar.height() * p0)
                    p.setPen(QPen(QColor(230, 230, 235)))
                    p.drawLine(bar.left() - 3, y0, bar.right() + 3, y0)
                p.setPen(QColor(230, 230, 235))
                p.drawText(QRect(r.left(), r.top() + 2, r.width(), 14), Qt.AlignCenter,
                           "M" if muted else (txt or "-∞"))
            else:
                if pos > 0:
                    col = QColor.fromHsvF(0.97, 0.80, 0.22 + 0.68 * pos)
                    if muted:
                        col = QColor(70, 70, 78)
                    p.fillRect(r.adjusted(2, 2, -2, -2), col)
                p.setPen(QColor(240, 240, 245) if pos > 0 else QColor(90, 90, 100))
                p.drawText(r, Qt.AlignCenter, "M" if muted else (txt or "·"))
            if self.win.target_label(n):
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(YELLOW))
                tr = r.topRight()
                p.drawPolygon(QPolygon([tr, tr + QPoint(-7, 0), tr + QPoint(0, 7)]))
            if self.win.learn_target == n:
                p.setPen(QPen(QColor(YELLOW), 2))
                p.setBrush(Qt.NoBrush)
                p.drawRect(r.adjusted(1, 1, -1, -1))
            elif here:
                p.setPen(QPen(QColor(200, 200, 210)))
                p.setBrush(Qt.NoBrush)
                p.drawRect(r.adjusted(0, 0, -1, -1))

        def cell_press(self, i, x, e):
            n, c = self.ctl(i, x)
            if c is None or self.win.learn_pick(n):
                return
            self.drag = (n, e.position().y(), fader_pos(c, c.value))

        def cell_drag(self, e):
            n, y0, p0 = self.drag
            c = card.ctls.get(n)
            if c is None:
                return
            k = 0.001 if e.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier) else 0.005
            raw = fader_raw(c, p0 + (y0 - e.position().y()) * k)
            if raw != c.value:
                self.win.apply(n, raw)

        def cell_double(self, i, x):
            n, c = self.ctl(i, x)
            if c is not None and c.db is not None:
                self.win.apply(n, c.from_db(0.0) if c.value == c.min else c.min)

        def cell_wheel(self, i, x, e):
            n, c = self.ctl(i, x)
            if c is None or c.db is None:
                return False
            d = 1 if e.angleDelta().y() > 0 else -1
            step = 0.5 if e.modifiers() & Qt.ControlModifier else 1.0
            db = c.to_db(c.value) if c.value > c.min else c.to_db(c.min)
            self.win.apply(n, c.from_db(db + d * step) if not (c.value == c.min and d < 0) else c.min)
            return True

        def cell_menu(self, i, x, gpos):
            n, c = self.ctl(i, x)
            if n:
                self.win.ctl_menu(n, gpos, mute=True)

        def cell_tip(self, i, x):
            n, c = self.ctl(i, x)
            if c is None:
                return ""
            src = card.ctls.get(f"Mixer Input {i:02d} Capture Enum")
            lab = self.win.target_label(n)
            return (f"In {i} ({src.text() if src else '?'}) → Mix {x}: {c.text()}"
                    + (" · mute" if n in self.win.softmute else "") + (f"\nMIDI: {lab}" if lab else "")
                    + "\nTrascina o rotella · Maiusc = fine · doppio clic 0 dB/-∞ · tasto destro: menu")

    # ------------------------------------------------------------------ finestra
    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(f"{APP} — {cname}")
            self.cfg = load_config()
            self.bridge = Bridge()
            self.bridge.loaded.connect(self._loaded)
            self.bridge.failed.connect(self._error)
            self.bridge.midi.connect(self.on_midi)
            self.worker = Worker(card, self.bridge)
            self.binders, self.any_binders, self.learn_w = {}, [], {}
            self.matrices, self.recent, self.softmute = [], {}, {}
            self.route_names, self.out_labels = [], {}
            self.last_err = 0
            self.learn_mode, self.learn_target = False, None
            mcfg = self.cfg.get("midi", {})
            self.midimap = {}
            for e in mcfg.get("map", []):
                try:
                    self.midimap[(e["type"], int(e["ch"]), int(e["num"]))] = e["target"]
                except (KeyError, ValueError, TypeError):
                    pass
            self.midi_prev, self.caught, self.midi_last = {}, set(), {}
            self.midi = MidiIn(lambda *a: self.bridge.midi.emit(*a))
            central = QWidget()
            self.setCentralWidget(central)
            root = QVBoxLayout(central)
            root.addLayout(self._top())
            self.tabs = QTabWidget()
            root.addWidget(self.tabs, 1)
            self.status = QLabel("")
            self.status.setStyleSheet("color:#9a9aa5;")
            root.addWidget(self.status)
            self._build()
            self._build_midi_tab()
            self.tabs.setCurrentIndex(min(int(self.cfg.get("ui", {}).get("tab", 0)), self.tabs.count() - 1))
            self.tabs.currentChanged.connect(lambda i: self._ui_cfg("tab", i))
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.worker.read_async)
            self.timer.start(1500)             # la manopola e i tasti della scheda si vedono qui
            self.midi_timer = QTimer(self)
            self.midi_timer.timeout.connect(self.midi_hotplug)
            self.midi_timer.start(4000)
            self.midi_open()
            QShortcut(QKeySequence("Esc"), self, activated=lambda: self.set_learn(False))
            QShortcut(QKeySequence("Ctrl+L"), self, activated=lambda: self.learn_btn.click())
            self.resize(1400, 880)

        # ------------------------------------------------------------ utilità
        def C(self, name):
            return card.ctls.get(name)

        def bind(self, name, fn):
            self.binders.setdefault(name, []).append(fn)
            fn(card.ctls)

        def apply(self, name, raw):
            """Scrive un valore: subito nell'interfaccia, poi alla scheda (in coda)."""
            c = card.ctls.get(name)
            if c is None or raw is None:
                return False
            if not c.writable:
                self._error(f"{pretty(name)}: in sola lettura (uscita su HW?)")
                return False
            raw = int(raw)
            if name in self.softmute and raw != c.min:
                self.softmute.pop(name, None)
            c.values = [raw] * max(1, c.count)
            self.recent[name] = time.monotonic()
            self.worker.set(name, raw)
            self.refresh(name)
            return True

        def refresh(self, name):
            for fn in self.binders.get(name, ()):
                fn(card.ctls)
            for fn in self.any_binders:
                fn()
            for m in self.matrices:
                m.viewport().update()

        def toggle(self, name, label, green=False, confirm=None):
            c = self.C(name)
            b = QPushButton(label)
            b.setCheckable(True)
            base = TOGGLE_GREEN if green else TOGGLE
            b.setStyleSheet(base)
            if c is None:
                b.setEnabled(False)
                return b

            def clicked(on):
                if self.learn_mode:
                    b.setChecked(not on)
                    self.learn_pick(name)
                    return
                if on and confirm and QMessageBox.question(self, "Conferma", confirm) != QMessageBox.Yes:
                    b.setChecked(False)
                    return
                self.apply(name, 1 if on else 0)
            b.clicked.connect(clicked)

            def bind(ctls, b=b, name=name):
                cc = ctls.get(name)
                if cc is not None and not b.isDown():
                    b.blockSignals(True)
                    b.setChecked(bool(cc.value))
                    b.setEnabled(cc.writable)
                    b.blockSignals(False)
            self.bind(name, bind)
            self.learnable(name, lambda st, lab: (b.setStyleSheet(base + LEARN_BTN[st]),
                                                  b.setToolTip(f"MIDI: {lab}" if lab else "")))
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda pt: self.ctl_menu(name, b.mapToGlobal(pt)))
            return b

        def mute_button(self, name):
            """Mute «software» per i livelli del mixer (la scheda non ha un mute per ogni ingresso del mix)."""
            b = QPushButton("M")
            b.setCheckable(True)
            b.setStyleSheet(TOGGLE)
            b.setToolTip("Mute (ricorda il livello)")
            t = "mute:" + name

            def clicked(on):
                if self.learn_mode:
                    b.setChecked(not on)
                    self.learn_pick(t)
                    return
                self.toggle_softmute(name, on)
            b.clicked.connect(clicked)

            def bind(ctls, b=b):
                b.blockSignals(True)
                b.setChecked(name in self.softmute)
                b.blockSignals(False)
            self.bind(name, bind)
            self.learnable(t, lambda st, lab: (b.setStyleSheet(TOGGLE + LEARN_BTN[st]),
                                               b.setToolTip(f"Mute · MIDI: {lab}" if lab else "Mute")))
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda pt: self.ctl_menu(t, b.mapToGlobal(pt)))
            return b

        def toggle_softmute(self, name, on=None):
            c = self.C(name)
            if c is None:
                return
            muted = name in self.softmute
            on = (not muted) if on is None else on
            if on and not muted:
                keep = c.value
                if self.apply(name, c.min):
                    self.softmute[name] = keep
            elif not on and muted:
                back = self.softmute.pop(name)
                if back is None or back == c.min:
                    back = c.from_db(0.0) if c.db else c.max
                self.apply(name, back)
            self.refresh(name)

        def combo(self, name, width=None):
            c = self.C(name)
            w = QComboBox()
            if width:
                w.setMinimumWidth(width)
            if c is None:
                w.setEnabled(False)
                return w
            w.addItems(c.items)
            w.setMaxVisibleItems(25)
            w.activated.connect(lambda i, name=name: self.apply(name, i))

            def bind(ctls, w=w, name=name):
                cc = ctls.get(name)
                if cc is not None and cc.value is not None and not w.view().isVisible():
                    w.blockSignals(True)
                    w.setCurrentIndex(cc.value)
                    w.setEnabled(cc.writable)
                    w.blockSignals(False)
            self.bind(name, bind)
            return w

        def fader(self, name, title, sub=""):
            c = self.C(name)
            f = Fader(c, title, sub)
            f.moved.connect(lambda raw, name=name: self.apply(name, raw))
            f.on_pick = lambda name=name: self.learn_pick(name)
            f.customContextMenuRequested.connect(lambda pt, f=f, name=name: self.ctl_menu(name, f.mapToGlobal(pt)))
            self.bind(name, lambda ctls, f=f, name=name: ctls.get(name) and f.update_from(ctls[name]))
            self.learnable(name, f.set_learn)
            return f

        @staticmethod
        def scroll(widget):
            sa = QScrollArea()
            sa.setWidgetResizable(True)
            sa.setFrameShape(QFrame.NoFrame)
            sa.setWidget(widget)
            return sa

        @staticmethod
        def hint(text):
            lab = QLabel(text)
            lab.setStyleSheet("color:#9a9aa5;")
            lab.setWordWrap(True)
            return lab

        def ctl_menu(self, target, gpos, mute=False):
            m = QMenu(self)
            c = self.C(target)
            if c is not None and c.type == "int" and c.db is not None and c.writable:
                lo, hi = c.to_db(c.min), c.to_db(c.max)
                for db in (0.0, -6.0, -12.0, -20.0):
                    if lo < db <= hi:
                        m.addAction(f"{db:+.0f} dB" if db else "0 dB", lambda db=db: self.apply(target, c.from_db(db)))
                m.addAction("-∞", lambda: self.apply(target, c.min))
                if mute:
                    m.addAction("Mute on/off", lambda: self.toggle_softmute(target))
                m.addSeparator()
            m.addAction("🎹  MIDI learn…", lambda: self.start_learn(target))
            if mute:
                m.addAction("🎹  MIDI learn del mute…", lambda: self.start_learn("mute:" + target))
            lab = self.target_label(target)
            if lab:
                m.addAction(f"Rimuovi assegnazione MIDI ({lab})", lambda: self.unmap_target(target))
            m.exec(gpos)

        def mix_dests(self, x):
            return [dest_label(n) for n in self.route_names if n in card.ctls and card.ctls[n].text() == f"Mix {x}"]

        def _ui_cfg(self, key, val):
            self.cfg.setdefault("ui", {})[key] = val
            save_config(self.cfg)

        def _matrix_state(self, m, key):
            st = self.cfg.get("ui", {}).get(key, {})
            m.collapsed_rows = set(st.get("rows", []))
            m.collapsed_cols = set(st.get("cols", []))
            m.expanded = {int(k) if isinstance(k, int) or str(k).isdigit() else k for k in st.get("exp", [])}
            m.relayout()
            m.on_layout = lambda: self._ui_cfg(key, {"rows": sorted(m.collapsed_rows), "cols": sorted(m.collapsed_cols),
                                                     "exp": sorted(m.expanded, key=str)})
            self.matrices.append(m)

        # ------------------------------------------------------------ barra in alto
        def _top(self):
            h = QHBoxLayout()
            h.addWidget(QLabel(f"<b style='font-size:16px'>{cname}</b>"))
            self.info = QLabel()
            self.info.setStyleSheet("color:#9a9aa5;")
            h.addWidget(self.info)
            h.addStretch(1)
            if self.C("Clock Source Clock Source"):
                h.addWidget(QLabel("Clock"))
                h.addWidget(self.combo("Clock Source Clock Source"))
            self.sync = QLabel()
            h.addWidget(self.sync)
            if self.C("Standalone Switch"):
                h.addWidget(self.toggle("Standalone Switch", "Standalone", green=True))
            for name, lab in (("Dim Playback Switch", "DIM"), ("Mute Playback Switch", "MUTE")):
                if self.C(name):
                    h.addWidget(self.toggle(name, lab))
            self.learn_btn = QPushButton("🎹 MIDI Learn")
            self.learn_btn.setCheckable(True)
            self.learn_btn.setToolTip("Ctrl+L · poi clicca un fader, un pulsante o una cella e muovi il "
                                      "controllo MIDI · Esc per uscire")
            self.learn_btn.setStyleSheet("QPushButton{background:#3a3c46;color:#ddd;border-radius:4px;padding:4px 10px;}"
                                         f"QPushButton:checked{{background:{YELLOW};color:black;font-weight:bold;}}")
            self.learn_btn.toggled.connect(self.set_learn)
            h.addWidget(self.learn_btn)
            pb = QPushButton("Preset ▾")
            m = QMenu(pb)
            m.addAction("Salva preset…", self.save_preset)
            m.addAction("Carica preset…", self.load_preset)
            pb.setMenu(m)
            h.addWidget(pb)
            return h

        # ------------------------------------------------------------ schede
        def _build(self):
            ctls = card.ctls
            used = set()
            outs = {}
            for n in ctls:
                m = re.match(r"^Line (\d+)\b(?:\s*\(([^)]*)\))?.*Playback Volume$", n)
                if m and "Volume Control" not in n:
                    outs[int(m.group(1))] = (n, m.group(2) or "")
                    if m.group(2):
                        self.out_labels[f"Out {int(m.group(1))}"] = m.group(2).replace("Headphones", "Cuffie")
            self.route_names = [n for n in sorted(ctls) if is_route(ctls[n])]

            # --- ingressi (modelli con controlli software degli ingressi)
            ins = [n for n in ctls if (re.match(r"^Line In \d", n) or "Phantom" in n or re.match(r"^Input \d", n))
                   and n not in self.route_names]
            if ins:
                w = QWidget()
                g = QGridLayout(w)
                for r, n in enumerate(sorted(ins)):
                    g.addWidget(QLabel(short_name(n)), r, 0)
                    c = ctls[n]
                    if c.type == "bool":
                        conf = "Accendere l'alimentazione phantom 48V?" if "phantom" in n.lower() else None
                        g.addWidget(self.toggle(n, "on", green=True, confirm=conf), r, 1, Qt.AlignLeft)
                    elif c.type == "enum":
                        g.addWidget(self.combo(n, 160), r, 1, Qt.AlignLeft)
                    else:
                        sl = QSlider(Qt.Horizontal)
                        sl.setRange(c.min, c.max)
                        sl.setValue(c.value or c.min)
                        sl.valueChanged.connect(lambda x, n=n: self.apply(n, x))
                        g.addWidget(sl, r, 1)
                    used.add(n)
                g.setRowStretch(len(ins), 1)
                g.setColumnStretch(2, 1)
                self.tabs.addTab(self.scroll(w), "Ingressi")

            # --- mixer (fader) e matrice dei mix
            names = {}
            for n in ctls:
                m = re.match(r"^Mix ([A-Z]) Input (\d+)", n)
                if m and ctls[n].type == "int":
                    names[(int(m.group(2)), m.group(1))] = n
            self.mixes = sorted({x for _, x in names})
            self.mix_inputs = sorted({i for i, _ in names})
            self.mix_names = names
            if names:
                used.update(names.values())
                self.mixtabs = QTabWidget()
                self.mixtabs.setDocumentMode(True)
                self.mix_built = set()
                for x in self.mixes:
                    page = QWidget()
                    QVBoxLayout(page).setContentsMargins(0, 4, 0, 0)
                    self.mixtabs.addTab(page, f"Mix {x}")
                self.mixtabs.currentChanged.connect(self._build_mix_page)
                self.any_binders.append(self._mix_tab_titles)
                self._build_mix_page(0)
                self.tabs.addTab(self.mixtabs, "Mixer")

                w = QWidget()
                v = QVBoxLayout(w)
                hb = QHBoxLayout()
                gm = GainMatrix(self, self.mix_inputs, self.mixes, names)
                for txt, fn in (("Espandi tutte le righe", lambda: gm.set_all_expanded(True)),
                                ("Comprimi tutte", lambda: gm.set_all_expanded(False))):
                    b = QPushButton(txt)
                    b.clicked.connect(fn)
                    hb.addWidget(b)
                hb.addStretch(1)
                hb.addWidget(self.hint("Clic sul nome di una riga: espandi/comprimi · trascina in verticale o "
                                       "rotella sulla cella · doppio clic 0 dB ↔ -∞ · tasto destro: valori, mute, MIDI"))
                v.addLayout(hb)
                v.addWidget(gm, 1)
                self._matrix_state(gm, "gainmatrix")
                self.tabs.addTab(w, "Matrice mix")
                used.update(n for n in ctls if n.startswith("Mixer Input "))

            # --- patch bay
            if self.route_names:
                w = QWidget()
                v = QVBoxLayout(w)
                hb = QHBoxLayout()
                pbm = PatchBay(self, self.route_names)
                for txt, fn in (("Uscite: PCM n → uscita n", lambda: self.direct_routing("out")),
                                ("Registrazione: ingresso n → PCM n", lambda: self.direct_routing("in")),
                                ("Apri tutti i gruppi", lambda: pbm.set_all_groups(True)),
                                ("Chiudi tutti", lambda: pbm.set_all_groups(False))):
                    b = QPushButton(txt)
                    b.clicked.connect(fn)
                    hb.addWidget(b)
                hb.addStretch(1)
                hb.addWidget(self.hint("Righe = destinazioni, colonne = sorgenti · clic: collega (di nuovo: Off) · "
                                       "clic sul titolo di un gruppo: apri/chiudi"))
                v.addLayout(hb)
                v.addWidget(pbm, 1)
                self._matrix_state(pbm, "patchbay")
                self.tabs.addTab(w, "Patch bay")
                used.update(self.route_names)

            # --- uscite
            if outs:
                w = QWidget()
                v = QVBoxLayout(w)
                hb = QHBoxLayout()
                for txt, val in (("Tutte le uscite su SW (volume da qui)", 0), ("Tutte su HW (manopola)", 1)):
                    b = QPushButton(txt)
                    b.clicked.connect(lambda _=False, val=val: self.all_hwsw(val))
                    hb.addWidget(b)
                hb.addStretch(1)
                hb.addWidget(self.hint("HW = volume dalla manopola Monitor della scheda (qui solo lettura) · "
                                       "SW = volume e mute da questo pannello"))
                v.addLayout(hb)
                strip = QHBoxLayout()
                for n in sorted(outs):
                    name, label = outs[n]
                    f = self.fader(name, f"Out {n}", label.replace("Headphones", "Cuffie"))
                    used.add(name)
                    mute = f"Line {n:02d} Mute Playback Switch"
                    if self.C(mute):
                        f.extra.addWidget(self.toggle(mute, "Mute"))
                        used.add(mute)
                    mode = f"Line Out {n:02d} Volume Control Playback Enum"
                    if self.C(mode):
                        f.extra.addWidget(self.combo(mode))
                        used.add(mode)
                    src = f"Analogue Output {n:02d} Playback Enum"
                    if self.C(src):
                        cb = self.combo(src)
                        cb.setToolTip("Sorgente di questa uscita (routing)")
                        f.extra.addWidget(cb)
                    strip.addWidget(f)
                strip.addStretch(1)
                v.addLayout(strip, 1)
                v.addWidget(self.hint("Sotto ogni uscita: mute · modo del volume (SW/HW) · sorgente"))
                self.tabs.addTab(self.scroll(w), "Uscite")

            # --- tutto il resto
            rest = [n for n in sorted(ctls) if n not in used and n not in
                    ("Clock Source Clock Source", "Standalone Switch", "Dim Playback Switch", "Mute Playback Switch")]
            self.rest = rest
            self._loaded(ctls)

        def _build_rest_tab(self):
            if not self.rest:
                return
            ctls = card.ctls
            w = QWidget()
            g = QGridLayout(w)
            for r, n in enumerate(self.rest):
                c = ctls[n]
                g.addWidget(QLabel(short_name(n)), r, 0)
                if c.type == "bool":
                    g.addWidget(self.toggle(n, "on", green=True), r, 1, Qt.AlignLeft)
                elif c.type == "enum":
                    g.addWidget(self.combo(n, 160), r, 1, Qt.AlignLeft)
                else:
                    lab = QLabel(c.text())

                    def bind(ct, lab=lab, n=n):
                        if n in ct:
                            lab.setText(ct[n].text() + ("" if ct[n].writable else "  (sola lettura)"))
                    self.bind(n, bind)
                    g.addWidget(lab, r, 1)
            g.setRowStretch(len(self.rest), 1)
            g.setColumnStretch(2, 1)
            self.tabs.addTab(self.scroll(w), "Altro")

        def _mix_tab_titles(self):
            for k, x in enumerate(self.mixes):
                d = self.mix_dests(x)
                self.mixtabs.setTabToolTip(k, f"Mix {x} → {', '.join(d)}" if d else f"Mix {x}: non instradato")
                lab = getattr(self, "mix_dest_labels", {}).get(x)
                if lab is not None:
                    lab.setText(f"<b>Mix {x}</b> → " + (", ".join(d) if d else
                                                         "<i>non instradato (collegalo a un'uscita nella Patch bay)</i>"))

        def _build_mix_page(self, idx):
            if idx < 0 or idx >= len(self.mixes) or idx in self.mix_built:
                return
            self.mix_built.add(idx)
            x = self.mixes[idx]
            page = self.mixtabs.widget(idx)
            v = page.layout()
            hb = QHBoxLayout()
            lab = QLabel()
            self.__dict__.setdefault("mix_dest_labels", {})[x] = lab
            hb.addWidget(lab)
            hb.addStretch(1)
            hb.addWidget(self.hint("Sotto ogni fader: mute · sorgente dell'ingresso del mixer · tasto destro: MIDI"))
            rb = QPushButton("Azzera questo mix (tutti a -∞)")
            rb.clicked.connect(lambda _=False, x=x: self.reset_mix(x))
            hb.addWidget(rb)
            v.addLayout(hb)
            inner = QWidget()
            strip = QHBoxLayout(inner)
            for i in self.mix_inputs:
                n = self.mix_names.get((i, x))
                if n is None:
                    continue
                src_name = f"Mixer Input {i:02d} Capture Enum"
                f = self.fader(n, f"In {i}", "")
                row = QHBoxLayout()
                row.addWidget(self.mute_button(n))
                f.extra.addLayout(row)
                if self.C(src_name):
                    cb = self.combo(src_name)
                    cb.setToolTip("Sorgente di questo ingresso del mixer (vale per tutti i mix)")
                    f.extra.addWidget(cb)
                    self.bind(src_name, lambda ct, f=f, s=src_name: f.sub.setText(ct[s].text() if s in ct else ""))
                strip.addWidget(f)
            strip.addStretch(1)
            v.addWidget(self.scroll(inner), 1)
            self._mix_tab_titles()
            self.refresh_learn()

        # ------------------------------------------------------------ MIDI
        def _build_midi_tab(self):
            self._build_rest_tab()
            w = QWidget()
            v = QVBoxLayout(w)
            be = self.midi.backend
            v.addWidget(QLabel("<b>Ingresso MIDI</b> — "
                               + (f"tramite {be}" if be else
                                  "<span style='color:#e66'>non disponibile: installa alsa-utils (aseqdump) "
                                  "oppure python-rtmidi</span>")))
            top = QHBoxLayout()
            left = QVBoxLayout()
            left.addWidget(QLabel("Porte da ascoltare (spunta il controller USB):"))
            self.port_list = QListWidget()
            self.port_list.setMaximumHeight(170)
            self.port_list.itemChanged.connect(self._port_toggled)
            left.addWidget(self.port_list)
            rb = QPushButton("Aggiorna l'elenco delle porte")
            rb.clicked.connect(self.fill_ports)
            left.addWidget(rb, 0, Qt.AlignLeft)
            top.addLayout(left, 1)
            right = QVBoxLayout()
            self.pickup_cb = QCheckBox("Pickup (soft takeover): il fader segue il controller solo dopo averlo "
                                       "«agganciato» — evita salti con controller non motorizzati")
            self.pickup_cb.setChecked(bool(self.cfg.get("midi", {}).get("pickup", False)))
            self.pickup_cb.toggled.connect(lambda on: self._midi_cfg())
            right.addWidget(self.pickup_cb)
            self.mon = QLabel("Ultimo messaggio: —")
            self.mon.setStyleSheet("font-family:monospace;background:#1a1b20;padding:6px;border-radius:4px;")
            right.addWidget(self.mon)
            right.addWidget(self.hint(
                "<b>MIDI learn</b>: premi «🎹 MIDI Learn» in alto (Ctrl+L), clicca il fader, il pulsante o la cella "
                "della matrice da comandare (si evidenzia in giallo), poi muovi il controllo sul controller. "
                "Puoi assegnare più controlli di fila; Esc per uscire. In alternativa: tasto destro su un fader → "
                "«MIDI learn…».<br>Fader: Control Change o Pitch Bend (fader in stile Mackie). Pulsanti e mute: "
                "Note o CC (ogni pressione commuta)."))
            right.addStretch(1)
            top.addLayout(right, 1)
            v.addLayout(top)
            v.addWidget(QLabel("<b>Assegnazioni</b>"))
            self.map_table = QTableWidget(0, 2)
            self.map_table.setHorizontalHeaderLabels(["Messaggio MIDI", "Controllo"])
            self.map_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
            self.map_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            self.map_table.verticalHeader().setVisible(False)
            self.map_table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.map_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            v.addWidget(self.map_table, 1)
            hb = QHBoxLayout()
            b1 = QPushButton("Rimuovi selezionate")
            b1.clicked.connect(self.remove_selected_maps)
            b2 = QPushButton("Cancella tutte")
            b2.clicked.connect(self.clear_maps)
            hb.addWidget(b1)
            hb.addWidget(b2)
            hb.addStretch(1)
            v.addLayout(hb)
            self.tabs.addTab(w, "MIDI")
            self.fill_ports()
            self.fill_map_table()

        def _midi_cfg(self):
            self.cfg["midi"] = {"ports": self.cfg.get("midi", {}).get("ports", []),
                                "pickup": self.pickup_cb.isChecked(),
                                "map": [{"type": k[0], "ch": k[1], "num": k[2], "target": t}
                                        for k, t in sorted(self.midimap.items())]}
            save_config(self.cfg)

        def fill_ports(self):
            want = self.cfg.get("midi", {}).get("ports", [])
            avail = self.midi.ports()
            self._avail = set(avail)
            self.port_list.blockSignals(True)
            self.port_list.clear()
            for p in avail + [p for p in want if p not in avail]:
                it = QListWidgetItem(p if p in avail else f"{p}   (non collegata)")
                it.setData(Qt.UserRole, p)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if p in want else Qt.Unchecked)
                self.port_list.addItem(it)
            self.port_list.blockSignals(False)

        def _port_toggled(self, _item):
            ports = [self.port_list.item(i).data(Qt.UserRole) for i in range(self.port_list.count())
                     if self.port_list.item(i).checkState() == Qt.Checked]
            self.cfg.setdefault("midi", {})["ports"] = ports
            self._midi_cfg()
            self.midi_open()

        def midi_open(self):
            want = self.cfg.get("midi", {}).get("ports", [])
            opened = self.midi.open(want)
            self._open_set = set(p for p in want if p in getattr(self, "_avail", set()))
            if want:
                self.status.setText(f"MIDI: {len(opened)} di {len(want)} porte aperte")

        def midi_hotplug(self):
            if not self.midi.backend:
                return
            avail = set(self.midi.ports())
            if avail != getattr(self, "_avail", set()):
                want = set(self.cfg.get("midi", {}).get("ports", []))
                changed = (avail & want) != (getattr(self, "_avail", set()) & want)
                self.fill_ports()
                if changed:
                    self.midi_open()

        def fill_map_table(self):
            self.map_table.setRowCount(0)
            for k, t in sorted(self.midimap.items(), key=lambda kv: pretty(kv[1])):
                r = self.map_table.rowCount()
                self.map_table.insertRow(r)
                a = QTableWidgetItem(midi_text(*k))
                a.setData(Qt.UserRole, list(k))
                self.map_table.setItem(r, 0, a)
                self.map_table.setItem(r, 1, QTableWidgetItem(pretty(t) + ("" if t.startswith("mute:") or t in card.ctls
                                                                           else "   (non presente su questa scheda)")))

        def remove_selected_maps(self):
            rows = {i.row() for i in self.map_table.selectedIndexes()}
            for r in rows:
                self.midimap.pop(tuple(self.map_table.item(r, 0).data(Qt.UserRole)), None)
            self._maps_changed()

        def clear_maps(self):
            if self.midimap and QMessageBox.question(self, "MIDI", "Cancellare tutte le assegnazioni MIDI?") \
                    == QMessageBox.Yes:
                self.midimap.clear()
                self._maps_changed()

        def unmap_target(self, target):
            self.midimap = {k: t for k, t in self.midimap.items() if t != target}
            self._maps_changed()

        def _maps_changed(self):
            self._midi_cfg()
            self.fill_map_table()
            self.refresh_learn()

        def target_label(self, target):
            for k, t in self.midimap.items():
                if t == target:
                    return midi_text(*k)
            return ""

        def learnable(self, target, fn):
            self.learn_w.setdefault(target, []).append(fn)
            fn(self._learn_state(target), self.target_label(target))

        def _learn_state(self, target):
            return "target" if target == self.learn_target else ("armed" if self.learn_mode else None)

        def refresh_learn(self):
            for t, fns in self.learn_w.items():
                st, lab = self._learn_state(t), self.target_label(t)
                for fn in fns:
                    fn(st, lab)
            for m in self.matrices:
                m.viewport().update()

        def set_learn(self, on):
            self.learn_mode = bool(on)
            if not on:
                self.learn_target = None
            if self.learn_btn.isChecked() != self.learn_mode:
                self.learn_btn.blockSignals(True)
                self.learn_btn.setChecked(self.learn_mode)
                self.learn_btn.blockSignals(False)
            self.status.setText("🎹 MIDI learn: clicca un fader, un pulsante o una cella, poi muovi il controllo "
                                "MIDI · Esc per uscire" if on else "MIDI learn terminato")
            self.status.setStyleSheet(f"color:{YELLOW};" if on else "color:#9a9aa5;")
            self.refresh_learn()

        def start_learn(self, target):
            self.set_learn(True)
            self.learn_pick(target)

        def learn_pick(self, target):
            """In modalità learn: sceglie il bersaglio e restituisce True (il clic non agisce sul controllo)."""
            if not self.learn_mode:
                return False
            self.learn_target = target
            self.status.setText(f"🎹 {pretty(target)}: ora muovi il controllo MIDI (Esc per annullare)")
            self.refresh_learn()
            return True

        def on_midi(self, port, kind, ch, num, val):
            key = (kind, ch, num)
            self.mon.setText(f"Ultimo messaggio: {midi_text(*key)}  valore {val:.2f}   ({port})")
            if self.learn_mode and self.learn_target:
                if kind == "note" and val == 0:
                    return
                t = self.learn_target
                c = self.C(t)
                if c is not None and c.type == "int" and kind == "note":
                    self.status.setText(f"🎹 {pretty(t)}: per un fader usa un fader/manopola (CC o pitch bend), "
                                        "non un tasto")
                    return
                self.midimap = {k: v for k, v in self.midimap.items() if v != t and k != key}
                self.midimap[key] = t
                self.caught.discard(key)
                self.midi_prev[key] = val
                self.learn_target = None
                self._maps_changed()
                self.status.setText(f"✔ {midi_text(*key)} → {pretty(t)}   · clicca il prossimo controllo o Esc")
                return
            t = self.midimap.get(key)
            prev = self.midi_prev.get(key)
            self.midi_prev[key] = val
            if not t:
                return
            pressed = val > 0 if kind == "note" else (val >= 0.5 and (prev is None or prev < 0.5))
            if t.startswith("mute:"):
                if pressed:
                    self.toggle_softmute(t[5:])
                return
            c = self.C(t)
            if c is None:
                return
            if c.type == "bool":
                if pressed:
                    self.apply(t, 0 if c.value else 1)
            elif c.type == "int" and kind != "note":
                cur = fader_pos(c, c.value)
                if self.pickup_cb.isChecked():
                    if key in self.caught and abs(cur - self.midi_last.get(key, cur)) > 0.03:
                        self.caught.discard(key)               # cambiato da un'altra parte: riaggancia
                    if key not in self.caught:
                        if abs(val - cur) < 0.03 or (prev is not None and (prev - cur) * (val - cur) <= 0):
                            self.caught.add(key)
                        else:
                            self.status.setText(f"Pickup {pretty(t)}: porta il controllo "
                                                f"{'su' if val < cur else 'giù'} fino al valore attuale")
                            return
                raw = fader_raw(c, val)
                if raw != c.value:
                    self.apply(t, raw)
                self.midi_last[key] = fader_pos(c, c.value)

        # ------------------------------------------------------------ azioni
        def reset_mix(self, x):
            if QMessageBox.question(self, "Mix", f"Portare a -∞ tutti gli ingressi del Mix {x}?") != QMessageBox.Yes:
                return
            for (i, xx), n in self.mix_names.items():
                if xx == x and n in card.ctls:
                    self.softmute.pop(n, None)
                    self.apply(n, card.ctls[n].min)

        def all_hwsw(self, val):
            what = "SW (volume e mute da questo pannello)" if val == 0 else "HW (manopola Monitor)"
            if QMessageBox.question(self, "Uscite", f"Mettere tutte le uscite su {what}?") != QMessageBox.Yes:
                return
            for n in card.ctls:
                if re.match(r"Line Out \d+ Volume Control", n):
                    self.apply(n, val)
            QTimer.singleShot(500, self.worker.read_async)

        def direct_routing(self, which):
            ctls = card.ctls
            todo = []
            for n in self.route_names:
                c = ctls[n]
                m = re.match(r"^Analogue Output (\d+) Playback Enum$" if which == "out" else r"^PCM (\d+) Capture Enum$", n)
                if m:
                    want = f"PCM {int(m.group(1))}" if which == "out" else f"Analogue {int(m.group(1))}"
                    if want in c.items:
                        todo.append((n, c.items.index(want)))
            if not todo or QMessageBox.question(self, "Routing", f"Impostare {len(todo)} instradamenti diretti?") \
                    != QMessageBox.Yes:
                return
            for n, i in todo:
                self.apply(n, i)

        def save_preset(self):
            path, _ = QFileDialog.getSaveFileName(self, "Salva preset", os.path.expanduser("~/scarlett-preset.json"),
                                                  "Preset (*.json)")
            if not path:
                return
            data = {"scheda": cname, "salvato": time.strftime("%Y-%m-%d %H:%M:%S"), "controlli": card.snapshot()}
            with open(path, "w") as f:
                json.dump(data, f, indent=1, ensure_ascii=False)
            self.status.setText(f"✔ Preset salvato: {path} ({len(data['controlli'])} controlli)")

        def load_preset(self):
            path, _ = QFileDialog.getOpenFileName(self, "Carica preset", os.path.expanduser("~"), "Preset (*.json)")
            if not path:
                return
            try:
                with open(path) as f:
                    data = json.load(f)
                self.timer.stop()
                with self.worker.busy:
                    fresh = Card(card.id)
                    fresh.read()
                    for n, c in fresh.ctls.items():         # stessi oggetti Control: i widget restano validi
                        if n in card.ctls:
                            card.ctls[n].values, card.ctls[n].writable = c.values, c.writable
                    n, errs = apply_preset(card, data)
            except (OSError, ValueError, AlsaError) as e:
                QMessageBox.warning(self, "Preset", str(e))
                return
            finally:
                self.timer.start(1500)
            self.softmute.clear()
            self.status.setText(f"✔ Preset caricato: {n} controlli cambiati"
                                + (f" · {len(errs)} non applicati (es. {errs[0]})" if errs else ""))
            self.worker.read_async()

        # ------------------------------------------------------------ aggiornamento
        def _loaded(self, ctls):
            now = time.monotonic()
            pend = self.worker.pending_names()
            for name, c in ctls.items():               # i controlli restano gli stessi oggetti per il Worker
                old = card.ctls.get(name)
                if old is None:
                    card.ctls[name] = c
                    continue
                if old is c:
                    continue
                old.writable = c.writable
                if name in pend or now - self.recent.get(name, 0) < 1.5:
                    continue
                old.values = c.values
                if name in self.softmute and old.value != old.min:
                    self.softmute.pop(name, None)
            for fns in self.binders.values():
                for b in fns:
                    b(card.ctls)
            for fn in self.any_binders:
                fn()
            for m in self.matrices:
                m.viewport().update()
            fw = card.ctls.get("Firmware Version")
            self.info.setText(f"  firmware {fw.value}" if fw and fw.value else "")
            s = card.ctls.get("Sync Status")
            if s is not None:
                ok = s.text() == "Locked"
                self.sync.setText(f"  ● {s.text()}  ")
                self.sync.setStyleSheet(f"color:{'#5c5' if ok else '#e66'};font-weight:bold;")
            if time.monotonic() - self.last_err > 5 and not self.learn_mode:
                self.status.setText(f"Scheda «{card.id}» · {len(card.ctls)} controlli · aggiornamento automatico"
                                    + (f" · MIDI: {len(self.midi.opened)} porte" if self.midi.opened else ""))

        def _error(self, msg):
            self.last_err = time.monotonic()
            self.status.setText(f"✘ {msg}")

        def closeEvent(self, e):
            self.midi.close()
            super().closeEvent(e)

    w = Window()
    w.show()
    if os.environ.get("DGFSCARLETT_SCREENSHOT"):          # per i test: salva le schede come immagini ed esce
        def shots():
            base = os.environ["DGFSCARLETT_SCREENSHOT"]
            for i in range(w.tabs.count()):
                w.tabs.setCurrentIndex(i)
                app.processEvents()
                w.grab().save(f"{base}-{i}-{w.tabs.tabText(i).replace(' ', '_')}.png")
            app.quit()
        QTimer.singleShot(800, shots)
    return app.exec()


VENVS = ["~/.local/share/dgfscarlett/.venv/bin/python", "~/.local/share/scarlett-control/.venv/bin/python",
         "~/.local/share/cuestage/.venv/bin/python"]


def ensure_pyside6():
    """Senza PySide6 nel Python in uso: riparte con quello dell'installazione (o di CueStage)."""
    import importlib.util
    if importlib.util.find_spec("PySide6") is not None:
        return
    if not os.environ.get("DGFSCARLETT_REEXEC"):
        for py in VENVS:
            py = os.path.expanduser(py)
            venv = os.path.dirname(os.path.dirname(py))           # (il python di un venv è un link a quello di sistema)
            if os.path.isfile(py) and os.access(py, os.X_OK) and os.path.abspath(sys.prefix) != os.path.abspath(venv):
                os.environ["DGFSCARLETT_REEXEC"] = "1"
                os.execv(py, [py, os.path.abspath(__file__)] + sys.argv[1:])
    print("PySide6 non trovato: per la finestra serve PySide6.\n"
          "  - installa con  ./install.sh  (crea un ambiente con PySide6), oppure\n"
          "  - sudo apt install python3-pyside6.qtwidgets   (Ubuntu 24.04 e successive), oppure\n"
          "  - pip install --user PySide6\n"
          "I comandi da terminale (list, get, set, save, load) funzionano anche senza.", file=sys.stderr)
    sys.exit(3)


def main():
    ap = argparse.ArgumentParser(description=f"{APP}: pannello di controllo per Focusrite Scarlett/Clarett (Linux)")
    ap.add_argument("--card", help="id ALSA della scheda (es. USB); predefinita: la prima Focusrite")
    ap.add_argument("cmd", nargs="?", choices=["list", "get", "set", "save", "load", "midi-ports"],
                    help="comando (senza: finestra)")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--version", action="version", version=f"{APP} {VERSION}")
    args, extra = ap.parse_known_args()
    bad = [x for x in extra if not re.fullmatch(r"-(\d+(\.\d+)?|inf)\s*(db)?", x.lower())]
    if bad:
        ap.error(f"argomenti non riconosciuti: {' '.join(bad)}")
    args.args += extra                     # valori negativi, es. «-20dB»
    try:
        if args.cmd:
            need = {"get": 1, "set": 2, "save": 1, "load": 1}.get(args.cmd, 0)
            if len(args.args) < need:
                ap.error(f"«{args.cmd}» richiede {need} argomenti")
            return cli(args)
        ensure_pyside6()
        return gui(args)
    except AlsaError as e:
        print(f"Errore: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
