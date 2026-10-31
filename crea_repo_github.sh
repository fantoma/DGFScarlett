#!/usr/bin/env bash
# Crea il repository GitHub di DGFScarlett e ci carica il progetto (una volta sola). Designed by Diego Fantoma.
#
#   ./crea_repo_github.sh                    pubblico, nome «DGFScarlett»
#   ./crea_repo_github.sh --privato          privato
#   ./crea_repo_github.sh --nome altro-nome  con un altro nome
#
# Serve lo strumento «gh» collegato al tuo account (gh auth login). Vedi GITHUB.md.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

R=$'\033[1;31m'; G=$'\033[1;32m'; C=$'\033[1;36m'; N=$'\033[0m'
say() { printf '%s==>%s %s\n' "$C" "$N" "$*"; }
die() { printf '%s ✘ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }
ask() { local a; read -r -p "$1 [s/N] " a; [[ "$a" =~ ^[sSyY] ]]; }

NAME="DGFScarlett"
VIS="--public"
while [ $# -gt 0 ]; do
    case "$1" in
        --privato|--private) VIS="--private" ;;
        --pubblico|--public) VIS="--public" ;;
        --nome) shift; NAME="${1:?manca il nome}" ;;
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "opzione sconosciuta: $1" ;;
    esac
    shift
done

command -v git >/dev/null || die "git non è installato: sudo apt install git"
command -v gh >/dev/null || die "gh non è installato: sudo apt install gh"
gh auth status >/dev/null 2>&1 || die "gh non è collegato a GitHub: gh auth login"
USER_GH="$(gh api user -q .login)"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    say "inizializzo il repository git"
    git init -q -b main
fi
[ -z "$(git config user.name || true)" ] && die "manca git config --global user.name \"Il tuo nome\""
[ -z "$(git config user.email || true)" ] && die "manca git config --global user.email \"la-tua-email\""
if [ -n "$(git status --porcelain)" ] || ! git rev-parse -q --verify HEAD >/dev/null; then
    say "salvo i file nel commit"
    git add -A
    git commit -q -m "DGFScarlett $(sed -n 's/^VERSION *= *"\(.*\)"/\1/p' dgfscarlett.py)"
fi
if git remote | grep -q .; then
    die "il progetto è già collegato a $(git remote get-url "$(git remote | head -1)"): usa ./release.sh"
fi
if gh repo view "$USER_GH/$NAME" >/dev/null 2>&1; then
    die "su GitHub esiste già $USER_GH/$NAME: scegli un altro nome con --nome"
fi

echo
echo "   Creo  https://github.com/$USER_GH/$NAME  (${VIS#--})  e carico il ramo main."
ask "Procedo?" || { echo "Annullato."; exit 1; }
gh repo create "$NAME" "$VIS" --source=. --remote=origin --push \
    --description "Pannello per Focusrite Scarlett / Clarett su Linux: patch bay, mixer a fader, vumeter, MIDI learn. Designed by Diego Fantoma"
printf '%s ✔%s fatto: https://github.com/%s/%s\n' "$G" "$N" "$USER_GH" "$NAME"
echo "   Il primo «Build» (test su Linux) parte da solo: https://github.com/$USER_GH/$NAME/actions"
echo "   Per pubblicare una versione con l'AppImage: ./release.sh"
