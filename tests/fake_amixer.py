#!/usr/bin/env python3
"""Finto «amixer» che simula una Scarlett 18i20 2ª gen. (driver scarlett2), per provare DGFScarlett senza
la scheda:  DGFSCARLETT_AMIXER=tests/fake_amixer.py ./dgfscarlett.py --card USB
Lo stato sta in $FAKE_AMIXER_STATE (predefinito /tmp/fake-scarlett.json)."""
import json
import math
import os
import sys
import time

STATE = os.environ.get("FAKE_AMIXER_STATE", "/tmp/fake-scarlett.json")
SOURCES = (["Off"] + [f"Analogue {i}" for i in range(1, 9)] + ["S/PDIF 1", "S/PDIF 2"]
           + [f"ADAT {i}" for i in range(1, 9)] + [f"Mix {chr(65 + i)}" for i in range(18)]
           + [f"PCM {i}" for i in range(1, 21)])
OUT_LABELS = {1: "Monitor L", 2: "Monitor R", 7: "Headphones 1 L", 8: "Headphones 1 R",
              9: "Headphones 2 L", 10: "Headphones 2 R"}


def controls():
    c, n = [], 0

    def add(name, kind, **kw):
        nonlocal n
        n += 1
        c.append(dict(numid=n, name=name, kind=kind, **kw))
    add("Clock Source Clock Source", "enum", items=["Internal", "S/PDIF", "ADAT"], value=0)
    add("Sync Status", "enum", items=["Unlocked", "Locked"], value=1, ro=True)
    add("Standalone Switch", "bool", value=0)
    add("Dim Playback Switch", "bool", value=0)
    add("Mute Playback Switch", "bool", value=0)
    for i in range(1, 11):
        lab = f" ({OUT_LABELS[i]})" if i in OUT_LABELS else ""
        add(f"Line {i:02d}{lab} Playback Volume", "int", min=0, max=127, value=127, db=(-127, 1), ro=(i <= 2))
        add(f"Line {i:02d} Mute Playback Switch", "bool", value=0)
        add(f"Line Out {i:02d} Volume Control Playback Enum", "enum", items=["SW", "HW"], value=1 if i <= 2 else 0)
    for i in range(1, 9):
        add(f"Line In {i} Level Capture Enum" if i <= 2 else f"Line In {i} Pad Capture Switch",
            "enum" if i <= 2 else "bool", items=["Line", "Inst"], value=0)
    for i in range(1, 11):
        add(f"Analogue Output {i:02d} Playback Enum", "enum", items=SOURCES, value=SOURCES.index(f"PCM {i}"))
    for i in range(1, 3):
        add(f"S/PDIF Output {i} Playback Enum", "enum", items=SOURCES, value=0)
    for i in range(1, 9):
        add(f"ADAT Output {i} Playback Enum", "enum", items=SOURCES, value=0)
    for i in range(1, 19):
        v = f"Analogue {i}" if i <= 8 else "Off"
        add(f"PCM {i:02d} Capture Enum", "enum", items=SOURCES, value=SOURCES.index(v))
    for i in range(1, 19):
        v = f"Analogue {i}" if i <= 8 else (f"PCM {i - 8}" if i <= 18 else "Off")
        add(f"Mixer Input {i:02d} Capture Enum", "enum", items=SOURCES, value=SOURCES.index(v))
    for x in range(18):
        for i in range(1, 19):
            add(f"Mix {chr(65 + x)} Input {i:02d} Playback Volume", "int", min=0, max=172,
                value=160 if i == x + 1 else 0, db=(-80, 0.5))
    add("Firmware Version", "int", min=0, max=99999, value=1083, ro=True)
    if not os.environ.get("FAKE_NO_METER"):
        add("Level Meter", "int", min=0, max=4095, value=0, ro=True)
    return c


DEST_ORDER = ([f"Analogue Output {i:02d} Playback Enum" for i in range(1, 11)]
              + [f"S/PDIF Output {i} Playback Enum" for i in range(1, 3)]
              + [f"ADAT Output {i} Playback Enum" for i in range(1, 9)]
              + [f"Mixer Input {i:02d} Capture Enum" for i in range(1, 19)]
              + [f"PCM {i:02d} Capture Enum" for i in range(1, 19)])
if os.environ.get("FAKE_OLD_KERNEL"):          # kernel < 6.7: ordine interno della scheda, PCM per primi
    DEST_ORDER = DEST_ORDER[38:] + DEST_ORDER[:38]


def level(src, t):
    """Segnale finto: gli ingressi analogici «respirano», l'1 arriva a saturare."""
    if src == "Off":
        return 0
    k = int(src.split()[-1]) if src.split()[-1].isdigit() else ord(src[-1]) - 64
    if src.startswith("Analogue"):
        db = -36 + 34 * abs(math.sin(t * (0.6 + 0.13 * k) + k)) + (2 if k == 1 else 0)
    elif src.startswith("PCM"):
        db = -24 + 10 * math.sin(t * 0.9 + k)
    else:
        db = -18 + 6 * math.sin(t + k)
    return int(4095 * 10 ** (min(0, db) / 20))


def load():
    try:
        with open(STATE) as f:
            vals = json.load(f)
    except (OSError, ValueError):
        vals = {}
    cs = controls()
    for c in cs:
        if c["name"] in vals:
            c["value"] = vals[c["name"]]
    # le uscite in HW non sono scrivibili
    modes = {c["name"][9:11]: c["value"] for c in cs if c["name"].startswith("Line Out ")}
    for c in cs:
        if c["name"].startswith("Line ") and "Playback Volume" in c["name"]:
            c["ro"] = modes.get(c["name"][5:7]) == 1
    by = {c["name"]: c for c in cs}
    t = time.time()
    if "Level Meter" in by:
        by["Level Meter"]["values"] = [level(by[d]["items"][by[d]["value"]], t) for d in DEST_ORDER]
    return cs


def dump(cs):
    out = []
    if len(cs) > 1:
        out += ["numid=900,iface=PCM,name='Playback Channel Map'", "  ; type=INTEGER,access=r----R--,values=20,min=0,max=36,step=0",
                "  : values=" + ",".join(["0"] * 20)]
    for c in cs:
        iface = "PCM" if c["name"] == "Level Meter" else "MIXER"      # come il driver vero
        out.append(f"numid={c['numid']},iface={iface},name='{c['name']}'")
        acc = "r-------" if c.get("ro") else "rw------"
        if c["kind"] == "int":
            vals = c.get("values", [c["value"]])
            out.append(f"  ; type=INTEGER,access={acc},values={len(vals)},min={c['min']},max={c['max']},step=0")
            out.append(f"  : values={','.join(map(str, vals))}")
            if "db" in c:
                out.append(f"  | dBscale-min={c['db'][0]:.2f}dB,step={c['db'][1]:.2f}dB,mute=0")
        elif c["kind"] == "bool":
            out.append(f"  ; type=BOOLEAN,access={acc},values=1")
            out.append(f"  : values={'on' if c['value'] else 'off'}")
        else:
            out.append(f"  ; type=ENUMERATED,access={acc},values=1,items={len(c['items'])}")
            for i, it in enumerate(c["items"]):
                out.append(f"  ; Item #{i} '{it}'")
            out.append(f"  : values={c['value']}")
    print("\n".join(out))


def main(argv):
    args = [a for a in argv if a not in ("-c", "USB")]
    cs = load()
    if args[:1] == ["contents"]:
        dump(cs)
        return 0
    if args[:1] == ["cget"]:
        numid = int(args[1].split("=")[1])
        dump([c for c in cs if c["numid"] == numid])
        return 0
    if args[:1] == ["cset"]:
        numid = int(args[1].split("=")[1])
        val = args[2].split(",")[0]
        c = next(c for c in cs if c["numid"] == numid)
        if c.get("ro"):
            print("amixer: Control USB element write error: Operation not permitted", file=sys.stderr)
            return 1
        try:
            with open(STATE) as f:
                vals = json.load(f)
        except (OSError, ValueError):
            vals = {}
        vals[c["name"]] = (1 if val == "on" else 0) if val in ("on", "off") else int(val)
        with open(STATE, "w") as f:
            json.dump(vals, f)
        return 0
    print("fake amixer: comando non gestito", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
