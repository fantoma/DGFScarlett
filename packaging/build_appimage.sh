#!/usr/bin/env bash
# Costruisce DGFScarlett-<versione>-x86_64.AppImage: un unico file eseguibile, portable, con dentro Python e Qt
# (PySide6). Designed by Diego Fantoma.
#
#   packaging/build_appimage.sh            (dalla cartella del progetto, su Ubuntu/Debian x86_64)
#
# Serve: bash, curl, tar, dpkg-deb e ldd. Nessun permesso di root richiesto.
# Il risultato funziona su distribuzioni con glibc >= 2.34: Ubuntu 22.04+, Debian 12+, Fedora 35+…
# Sul computer di destinazione servono solo alsa-utils (amixer, aseqdump), presenti quasi ovunque.
# I download restano in build/cache. Variabili: PY_URL, APPIMAGETOOL_URL, KEEP_APPDIR=1.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
CACHE="$BUILD/cache"
APPDIR="$BUILD/DGFScarlett.AppDir"
VERSION="$(sed -n 's/^VERSION *= *"\(.*\)"/\1/p' "$ROOT/dgfscarlett.py")"
VERSION="${VERSION:-dev}"
OUT="$ROOT/dist/DGFScarlett-${VERSION}-x86_64.AppImage"

PY_URL="${PY_URL:-https://github.com/astral-sh/python-build-standalone/releases/download/20251014/cpython-3.12.12+20251014-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz}"
APPIMAGETOOL_URL="${APPIMAGETOOL_URL:-https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage}"

say() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
fetch() {  # fetch URL FILE
    [ -s "$CACHE/$2" ] || { say "scarico $2"; curl -fL --retry 3 -o "$CACHE/$2.part" "$1"; mv "$CACHE/$2.part" "$CACHE/$2"; }
}

mkdir -p "$CACHE" "$ROOT/dist"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/app"

# ---------------------------------------------------------------- Python portabile
fetch "$PY_URL" python.tar.gz
say "Python portabile"
tar -xzf "$CACHE/python.tar.gz" -C "$APPDIR/usr"          # -> usr/python
PY="$APPDIR/usr/python/bin/python3"
rm -rf "$APPDIR"/usr/python/lib/python3.*/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3} \
       "$APPDIR"/usr/python/lib/{libtcl,libtk}* "$APPDIR"/usr/python/lib/{tcl,tk,itcl,thread}* 2>/dev/null || true

# ---------------------------------------------------------------- Qt (solo la parte «Essentials» di PySide6)
say "PySide6"
"$PY" -m pip install --no-warn-script-location --disable-pip-version-check -q PySide6-Essentials
"$PY" "$ROOT/packaging/prune_qt.py" "$APPDIR/usr/python"
"$PY" -m pip uninstall -y -q pip setuptools 2>/dev/null || true

# ---------------------------------------------------------------- xcb-cursor (estratto, non installato)
say "xcb-cursor"
DEBS="$BUILD/debs"; mkdir -p "$DEBS" && rm -rf "${DEBS:?}"/*
u=universe/x/xcb-util-cursor/libxcb-cursor0_0.1.1-4ubuntu1_amd64.deb
fetch "http://archive.ubuntu.com/ubuntu/pool/$u" "$(basename "$u")"
dpkg-deb -x "$CACHE/$(basename "$u")" "$DEBS"
mkdir -p "$APPDIR/usr/lib/fallback/xcb"
cp -a "$DEBS"/usr/lib/x86_64-linux-gnu/libxcb-cursor.so.0* "$APPDIR/usr/lib/fallback/xcb/"

# ---------------------------------------------------------------- DGFScarlett
say "DGFScarlett $VERSION"
cp "$ROOT/dgfscarlett.py" "$ROOT/LICENSE" "$APPDIR/usr/app/"
find "$APPDIR" -name __pycache__ -prune -exec rm -rf {} +
"$PY" -m compileall -q -j0 "$APPDIR/usr/app" "$APPDIR/usr/python/lib" >/dev/null || true
cp "$ROOT/packaging/AppRun" "$APPDIR/AppRun" && chmod +x "$APPDIR/AppRun"
cp "$ROOT/packaging/dgfscarlett.desktop" "$APPDIR/"
cp "$ROOT/packaging/dgfscarlett.png" "$APPDIR/dgfscarlett.png"
ln -sf dgfscarlett.png "$APPDIR/.DirIcon"

# ---------------------------------------------------------------- prova rapida dentro l'AppDir
say "prova di avvio (senza schermo)"
QT_QPA_PLATFORM=offscreen "$APPDIR/AppRun" --self-test

# ---------------------------------------------------------------- AppImage
fetch "$APPIMAGETOOL_URL" appimagetool
chmod +x "$CACHE/appimagetool"
say "creo $(basename "$OUT")"
rm -f "$OUT"
ARCH=x86_64 "$CACHE/appimagetool" --appimage-extract-and-run --no-appstream -n "$APPDIR" "$OUT" >/dev/null
( cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" )
[ "${KEEP_APPDIR:-0}" = 1 ] || rm -rf "$APPDIR" "$DEBS"
say "fatto: $OUT ($(du -h "$OUT" | cut -f1))"
