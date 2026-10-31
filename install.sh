#!/usr/bin/env bash
# DGFScarlett — Designed by Diego Fantoma
# Installazione per l'utente corrente (nessun permesso di root richiesto, tranne i pacchetti apt).
#   ./install.sh             installa o aggiorna
#   ./install.sh --uninstall rimuove programma, ambiente e collegamenti (la configurazione resta)
set -euo pipefail

APP=dgfscarlett
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/$APP"
BIN="$HOME/.local/bin/$APP"
DESKTOP="$HOME/.local/share/applications/$APP.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -rf "$DEST" "$BIN" "$DESKTOP"
    echo "DGFScarlett rimosso. La configurazione (assegnazioni MIDI) è in ~/.config/dgfscarlett: cancellala a mano se vuoi."
    exit 0
fi

need=()
command -v amixer   >/dev/null || need+=(alsa-utils)
python3 -c "import venv, ensurepip" 2>/dev/null || need+=(python3-venv)
if ((${#need[@]})); then
    echo "Servono i pacchetti: ${need[*]}"
    sudo apt install -y "${need[@]}"
fi

mkdir -p "$DEST" "$(dirname "$BIN")" "$(dirname "$DESKTOP")"
install -m 755 "$SRC/dgfscarlett.py" "$DEST/dgfscarlett.py"

if [[ ! -x "$DEST/.venv/bin/python" ]]; then
    echo "Creo l'ambiente Python con PySide6 (una volta sola, qualche minuto)…"
    python3 -m venv "$DEST/.venv"
fi
"$DEST/.venv/bin/pip" install -q --upgrade pip
"$DEST/.venv/bin/pip" install -q PySide6
# facoltativo: ingresso MIDI diretto (senza, si usa «aseqdump» di alsa-utils)
"$DEST/.venv/bin/pip" install -q python-rtmidi 2>/dev/null \
    || echo "  (python-rtmidi non installato: per il MIDI si userà aseqdump — va bene uguale)"

cat > "$BIN" <<EOF
#!/bin/sh
exec "$DEST/.venv/bin/python" "$DEST/dgfscarlett.py" "\$@"
EOF
chmod 755 "$BIN"

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=DGFScarlett
GenericName=Mixer Focusrite Scarlett
Comment=Patch bay, mixer e MIDI learn per Focusrite Scarlett / Clarett — Designed by Diego Fantoma
Exec=$BIN
Icon=audio-card
Terminal=false
Categories=AudioVideo;Audio;Mixer;
EOF

echo "✔ Installato. Avvia con «dgfscarlett» o dal menu delle applicazioni."
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "  Nota: aggiungi ~/.local/bin al PATH per usare il comando da terminale.";; esac
