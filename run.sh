#!/usr/bin/env bash
# DGFScarlett — Designed by Diego Fantoma
# Avvio direttamente dalla cartella del progetto, senza installare.
#   ./run.sh                 apre la finestra
#   ./run.sh list Mix        qualsiasi comando da terminale (list, get, set, save, load, midi-ports)
#   ./run.sh --card USB      sceglie la scheda
# Python con PySide6, nell'ordine: .venv del progetto → ambiente di install.sh → Python di sistema.
# Se non c'è da nessuna parte, crea .venv nel progetto (una volta sola).
set -euo pipefail

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SCRIPT="$DIR/dgfscarlett.py"

has_pyside() { "$1" -c "import PySide6" >/dev/null 2>&1; }

# i comandi da terminale non hanno bisogno di PySide6 (anche dopo «--card ID»)
for a in "$@"; do
    case "$a" in
        list|get|set|save|load|midi-ports|-h|--help|--version)
            exec python3 "$SCRIPT" "$@" ;;
    esac
done

if [[ -z "${DGFSCARLETT_AMIXER:-}" ]] && ! command -v amixer >/dev/null; then
    echo "Manca «amixer»: installa alsa-utils  (sudo apt install alsa-utils)" >&2
    exit 1
fi

PY=""
for cand in "$DIR/.venv/bin/python" "$HOME/.local/share/dgfscarlett/.venv/bin/python" python3; do
    if command -v "$cand" >/dev/null 2>&1 && has_pyside "$cand"; then
        PY="$cand"
        break
    fi
done

if [[ -z "$PY" ]]; then
    echo "PySide6 non trovato: creo l'ambiente in $DIR/.venv (una volta sola, qualche minuto)…"
    if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
        echo "Serve python3-venv:  sudo apt install python3-venv" >&2
        exit 1
    fi
    python3 -m venv "$DIR/.venv"
    "$DIR/.venv/bin/pip" install -q --upgrade pip
    PYSIDE="PySide6"
    # PySide6 6.12 ha un difetto con Python ≤ 3.11 (crash «deallocating None»): lì si usa la 6.11.3
    "python3" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || PYSIDE="PySide6==6.11.3"
    "$DIR/.venv/bin/pip" install -q "$PYSIDE"
    PY="$DIR/.venv/bin/python"
fi

export DGFSCARLETT_REEXEC=1          # l'interprete è già quello giusto: niente rilancio
exec "$PY" "$SCRIPT" "$@"
