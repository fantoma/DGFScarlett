"""Toglie da PySide6 le parti che DGFScarlett non usa (WebEngine, Qt Quick, 3D, Designer, rete…).

    python prune_qt.py <cartella del Python portabile>

Tiene i moduli in KEEP e, delle librerie Qt, solo quelle che servono (con ldd) a questi moduli
e ai plugin conservati. Riduce PySide6 a meno di un terzo.
"""
import glob
import os
import re
import shutil
import subprocess
import sys

KEEP = {"QtCore", "QtGui", "QtWidgets", "QtDBus"}
KEEP_PLUGINS = {"platforms", "platformthemes", "platforminputcontexts", "xcbglintegrations", "egldeviceintegrations",
                "wayland-decoration-client", "wayland-graphics-integration-client", "wayland-shell-integration",
                "imageformats", "iconengines", "tls", "networkinformation", "generic"}

DROP_PLUGIN_FILES = ("platforminputcontexts/libqtvirtualkeyboardplugin.so", "imageformats/libqpdf.so")


def size(path):
    tot = 0
    for d, _, files in os.walk(path):
        for f in files:
            p = os.path.join(d, f)
            if not os.path.islink(p):
                tot += os.path.getsize(p)
    return tot


def rm(p):
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p, ignore_errors=True)
    elif os.path.lexists(p):
        os.remove(p)


def needed(files, libdir):
    """Librerie di libdir richieste (in modo transitivo) dai file indicati."""
    seen, todo = set(), list(files)
    env = dict(os.environ, LD_LIBRARY_PATH=libdir)
    while todo:
        f = todo.pop()
        try:
            out = subprocess.run(["ldd", f], capture_output=True, text=True, env=env).stdout
        except OSError:
            continue
        for m in re.finditer(r"=>\s+(\S+)", out):
            p = os.path.realpath(m.group(1))
            if p.startswith(os.path.realpath(libdir)) and p not in seen:
                seen.add(p)
                todo.append(p)
    return seen


def main(pyroot):
    site = glob.glob(os.path.join(pyroot, "lib", "python3.*", "site-packages"))[0]
    ps = os.path.join(site, "PySide6")
    qt = os.path.join(ps, "Qt")
    before = size(site)

    for f in glob.glob(os.path.join(ps, "Qt*.abi3.so")) + glob.glob(os.path.join(ps, "Qt*.pyi")):
        if os.path.basename(f).split(".")[0] not in KEEP:
            rm(f)
    for name in ("examples", "include", "typesystems", "glue", "scripts", "doc", "support/generate_pyi.py",
                 "assistant", "designer", "linguist", "lupdate", "lrelease", "qmlformat", "qmllint", "qmlls",
                 "balsam", "balsamui", "Designer.app", "qmltyperegistrar", "uic", "rcc", "qsb", "svgtoqml"):
        rm(os.path.join(ps, name))
    for name in ("qml", "libexec", "resources", "metatypes", "modules", "translations/qtwebengine_locales"):
        rm(os.path.join(qt, name))
    for f in glob.glob(os.path.join(qt, "translations", "*")):
        b = os.path.basename(f)
        if not re.match(r"qt(base)?_(it|en|sl)\.qm$", b) and os.path.isfile(f):
            rm(f)
    plugdir = os.path.join(qt, "plugins")
    for d in os.listdir(plugdir):
        if d not in KEEP_PLUGINS:
            rm(os.path.join(plugdir, d))
    for f in DROP_PLUGIN_FILES:              # trascinerebbero Qt Quick/QML e Qt PDF
        rm(os.path.join(plugdir, f))
    # librerie Qt: solo quelle raggiunte dai moduli e dai plugin rimasti
    libdir = os.path.join(qt, "lib")
    roots = glob.glob(os.path.join(ps, "*.so*")) + glob.glob(os.path.join(plugdir, "**", "*.so"), recursive=True)
    roots += glob.glob(os.path.join(site, "shiboken6", "*.so*"))
    keep = needed(roots, libdir)
    for f in glob.glob(os.path.join(libdir, "*")):
        if os.path.isfile(f) and os.path.realpath(f) not in keep:
            rm(f)
    # PySide6-Addons: se ne resta qualcosa di inutile (pacchetti vuoti), lo lasciamo: pesa poco
    for d in glob.glob(os.path.join(site, "**", "__pycache__"), recursive=True):
        rm(d)
    after = size(site)
    print(f"PySide6 ridotto: {before / 1e6:.0f} MB -> {after / 1e6:.0f} MB")


if __name__ == "__main__":
    main(sys.argv[1])
