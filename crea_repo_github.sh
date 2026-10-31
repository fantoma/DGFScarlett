#!/usr/bin/env bash
# Crea il repository GitHub di DGFScarlett e ci carica il progetto (una volta sola). Designed by Diego Fantoma.
#
#   ./crea_repo_github.sh                    pubblico, nome «DGFScarlett»
#   ./crea_repo_github.sh --privato          privato
#   ./crea_repo_github.sh --nome altro-nome  con un altro nome
#
# Se il repository esiste già su GitHub, propone di collegarci questa cartella: se lassù c'è una storia
# diversa, la conserva nel ramo «main-precedente» prima di aggiornare «main».
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
        -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "opzione sconosciuta: $1" ;;
    esac
    shift
done

Y=$'\033[1;33m'
warn() { printf '%s !%s %s\n' "$Y" "$N" "$*"; }

collega() {   # il repository esiste già: ci collega questa cartella
    local repo="$1" url="${DGF_REMOTE_URL:-https://github.com/$1.git}" remote_main
    echo
    warn "su GitHub esiste già $repo"
    ask "Collego questa cartella a $repo e ci carico il progetto?" \
        || die "annullato. Per creare un repository nuovo: ./crea_repo_github.sh --nome altro-nome"
    git remote add origin "$url"
    say "leggo cosa c'è su GitHub"
    git fetch -q origin || { git remote remove origin; die "GitHub non risponde o non ho accesso a $repo"; }
    remote_main="$(git rev-parse -q --verify origin/main || true)"
    if [ -z "$remote_main" ]; then
        say "il repository è vuoto: carico il ramo main"
        git push -q -u origin main
    elif git merge-base --is-ancestor "$remote_main" HEAD; then
        say "su GitHub c'è una versione precedente di questo progetto: la aggiorno"
        git push -q -u origin main
    elif git merge-base --is-ancestor HEAD "$remote_main"; then
        git branch -q -u origin/main main 2>/dev/null || true
        warn "su GitHub c'è una versione più recente di questa cartella: prima «git pull», poi ./release.sh"
        return 0
    else
        echo
        warn "su GitHub «main» ha una storia diversa da questa cartella (es. caricata da un altro zip):"
        git log --oneline -3 origin/main | sed 's/^/      GitHub: /'
        git log --oneline -3 HEAD | sed 's/^/      qui:    /'
        echo "   Se procedo, su GitHub «main» diventa quella di questa cartella e la versione attuale"
        echo "   resta nel ramo «main-precedente» (non si perde nulla)."
        ask "Procedo?" || { git remote remove origin; die "annullato: niente è stato cambiato su GitHub"; }
        git push -q origin "origin/main:refs/heads/main-precedente"
        git push -q --force-with-lease=main:"$remote_main" -u origin main
        printf '%s ✔%s la versione precedente è nel ramo main-precedente\n' "$G" "$N"
    fi
    git push -q origin --tags 2>/dev/null || true
    printf '%s ✔%s fatto: https://github.com/%s\n' "$G" "$N" "$repo"
    echo "   Il «Build» (test su Linux) parte da solo: https://github.com/$repo/actions"
    echo "   Per pubblicare una versione con l'AppImage: ./release.sh"
}

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
    collega "$USER_GH/$NAME"
    exit 0
fi

echo
echo "   Creo  https://github.com/$USER_GH/$NAME  (${VIS#--})  e carico il ramo main."
ask "Procedo?" || { echo "Annullato."; exit 1; }
gh repo create "$NAME" "$VIS" --source=. --remote=origin --push \
    --description "Pannello per Focusrite Scarlett / Clarett su Linux: patch bay, mixer a fader, vumeter, MIDI learn. Designed by Diego Fantoma"
printf '%s ✔%s fatto: https://github.com/%s/%s\n' "$G" "$N" "$USER_GH" "$NAME"
echo "   Il primo «Build» (test su Linux) parte da solo: https://github.com/$USER_GH/$NAME/actions"
echo "   Per pubblicare una versione con l'AppImage: ./release.sh"
