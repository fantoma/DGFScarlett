#!/usr/bin/env bash
# Pubblica una nuova versione di DGFScarlett su GitHub. Designed by Diego Fantoma.
#
#   ./release.sh              chiede il numero di versione
#   ./release.sh 1.0.1        usa quello indicato
#   ./release.sh --prova      mostra cosa farebbe, senza cambiare né inviare nulla
#
# Cosa fa:
#   1. controlla git, il collegamento a GitHub e che la versione sia nuova
#   2. (facoltativo) esegue i test di regressione in locale
#   3. scrive la versione nei file, fa il commit e lo invia a GitHub
#   4. crea il tag «vX.Y.Z» e lo invia: GitHub Actions costruisce l'AppImage e pubblica la Release
#   5. se c'è lo strumento «gh», segue la costruzione e alla fine mostra il link alla Release
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
INIT="dgfscarlett.py"

R=$'\033[1;31m'; G=$'\033[1;32m'; Y=$'\033[1;33m'; C=$'\033[1;36m'; N=$'\033[0m'
say()  { printf '%s==>%s %s\n' "$C" "$N" "$*"; }
ok()   { printf '%s ✔%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s !%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '%s ✘ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }
ask()  { local a; read -r -p "$1 [s/N] " a; [[ "$a" =~ ^[sSyY] ]]; }

PROVA=0
VERS=""
for a in "$@"; do
    case "$a" in
        --prova|--dry-run) PROVA=1 ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) VERS="$a" ;;
    esac
done
run() {   # esegue, oppure in prova lo mostra soltanto
    if [ "$PROVA" = 1 ]; then printf '   (prova) %s\n' "$*"; else "$@"; fi
}

# ---------------------------------------------------------------- controlli
command -v git >/dev/null || die "git non è installato: sudo apt install git"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || die "questa cartella non è ancora un repository git: segui il punto 2 di GITHUB.md"
REMOTE="$(git remote | head -1)"
[ -n "$REMOTE" ] || die "nessun collegamento a GitHub (git remote): segui il punto 2 di GITHUB.md"
BRANCH="$(git branch --show-current)"
[ -n "$BRANCH" ] || die "non sei su un ramo (HEAD staccato): git switch main"
[ "$BRANCH" = "main" ] || warn "sei sul ramo «$BRANCH», non su «main»: la versione partirà comunque dal tag"

CUR="$(sed -n 's/^VERSION *= *"\(.*\)"/\1/p' "$INIT")"
[ -n "$CUR" ] || die "versione attuale non trovata in $INIT"

say "aggiorno le informazioni da GitHub ($REMOTE)"
git fetch --quiet --tags "$REMOTE" || die "GitHub non risponde: controlla la connessione (o «gh auth login»)"
if git rev-parse --verify --quiet "$REMOTE/$BRANCH" >/dev/null; then
    BEHIND="$(git rev-list --count "HEAD..$REMOTE/$BRANCH")"
    [ "$BEHIND" = 0 ] || die "su GitHub ci sono $BEHIND commit che qui mancano: prima «git pull»"
fi
LAST_TAG="$(git tag -l 'v*' --sort=-v:refname | head -1)"
echo "   versione nei file:     $CUR"
echo "   ultima versione su GitHub: ${LAST_TAG:-nessuna}"

# ---------------------------------------------------------------- nuova versione
suggest() {   # X.Y.Z -> X.Y.(Z+1)
    local IFS=.; read -r a b c <<<"$1"; echo "$a.$b.$((c + 1))"
}
BASE="${LAST_TAG#v}"; BASE="${BASE:-$CUR}"
newer() {     # newer A B: vero se A > B
    [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)" = "$1" ]
}
if [ -z "$LAST_TAG" ] || newer "$CUR" "$BASE"; then DEF="$CUR"; else DEF="$(suggest "$BASE")"; fi
if [ -z "$VERS" ]; then
    read -r -p "Nuova versione [$DEF]: " VERS
    VERS="${VERS:-$DEF}"
fi
VERS="${VERS#v}"
[[ "$VERS" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "«$VERS» non va bene: usa tre numeri, es. 1.2.3"
TAG="v$VERS"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "il tag $TAG esiste già: scegli un altro numero"
git ls-remote --exit-code --tags "$REMOTE" "refs/tags/$TAG" >/dev/null 2>&1 && die "il tag $TAG è già su GitHub"
[ -z "$LAST_TAG" ] || newer "$VERS" "${LAST_TAG#v}" \
    || { warn "$VERS non è maggiore dell'ultima versione pubblicata (${LAST_TAG#v})"; ask "Continuo lo stesso?" || exit 1; }

read -r -p "Breve descrizione delle novità (facoltativa): " NOTE

# ---------------------------------------------------------------- riepilogo
echo
say "riepilogo"
echo "   versione:  $CUR → $VERS   (tag $TAG)"
echo "   ramo:      $BRANCH → $REMOTE"
CHANGES="$(git status --short)"
if [ -n "$CHANGES" ]; then
    echo "   modifiche non ancora salvate che entreranno nel commit:"
    printf '%s\n' "$CHANGES" | sed 's/^/      /' | head -25
    [ "$(printf '%s\n' "$CHANGES" | wc -l)" -le 25 ] || echo "      …"
fi
echo
[ "$PROVA" = 1 ] || ask "Procedo?" || { echo "Annullato: niente è stato cambiato."; exit 1; }

# ---------------------------------------------------------------- test (facoltativi)
PYBIN=""
for p in .venv/bin/python "$HOME/.local/share/dgfscarlett/.venv/bin/python" python3; do
    if [ -x "$p" ] || command -v "$p" >/dev/null 2>&1; then
        "$p" -c "import PySide6" 2>/dev/null && { PYBIN="$p"; break; }
    fi
done
[ -n "$PYBIN" ] || { PYBIN=python3; warn "Python con PySide6 non trovato: in locale salto la prova della finestra"; }
if [ "$PROVA" = 1 ] || ask "Eseguo prima i test in locale (circa 10 secondi, con la scheda simulata)?"; then
    say "test"
    if [ "$PROVA" = 1 ]; then
        printf '   (prova) %s -m unittest discover -s tests\n' "$PYBIN"
    elif ! QT_QPA_PLATFORM=offscreen DGFSCARLETT_MIDI=off "$PYBIN" -m unittest discover -s tests; then
        ask "Alcuni test sono falliti. Pubblico lo stesso?" || die "annullato: niente è stato cambiato"
    fi
fi

# ---------------------------------------------------------------- file, commit, tag
say "scrivo la versione $VERS"
run sed -i "s/^VERSION *= *\".*\"/VERSION = \"$VERS\"/" "$INIT"

MSG="Versione $VERS"
[ -z "$NOTE" ] || MSG="$MSG: $NOTE"
say "commit"
run git add -A
if [ "$PROVA" = 1 ] || ! git diff --cached --quiet; then
    run git commit -q -m "$MSG"
else
    warn "nessuna modifica da salvare (la versione era già $VERS)"
fi
run git tag -a "$TAG" -m "DGFScarlett $VERS${NOTE:+ — $NOTE}"

say "invio a GitHub"
run git push -q "$REMOTE" "$BRANCH"
run git push -q "$REMOTE" "$TAG"

if [ "$PROVA" = 1 ]; then
    echo; ok "prova finita: niente è stato cambiato né inviato"; exit 0
fi
ok "versione $VERS inviata: GitHub Actions ora prova e costruisce l'AppImage (5–10 minuti)"

# ---------------------------------------------------------------- segue la costruzione (se c'è gh)
URL="$(git remote get-url "$REMOTE" | sed -E 's#^git@github.com:#https://github.com/#; s#\.git$##')"
if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
    if ask "Vuoi seguire qui l'andamento della costruzione?"; then
        say "attendo che GitHub avvii il lavoro…"
        RUN=""
        for _ in $(seq 1 30); do
            RUN="$(gh run list --workflow build.yml --branch "$TAG" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || true)"
            [ -n "$RUN" ] && break
            sleep 4
        done
        if [ -n "$RUN" ]; then
            if gh run watch "$RUN" --interval 15 --exit-status; then
                ok "costruzione riuscita"
                echo "   Release: $URL/releases/tag/$TAG"
                gh release view "$TAG" --json assets -q '.assets[].name' 2>/dev/null | sed 's/^/   • /'
            else
                printf '%s ✘%s qualcosa non è andato: %s\n' "$R" "$N" "$URL/actions/runs/$RUN"
                echo "   (per riprovare dopo una correzione: elimina il tag con"
                echo "    git tag -d $TAG && git push $REMOTE :refs/tags/$TAG  e rilancia ./release.sh $VERS)"
                exit 1
            fi
            exit 0
        fi
        warn "il lavoro non è ancora comparso: guardalo su $URL/actions"
    fi
fi
echo "   Andamento: $URL/actions"
echo "   Release (quando è pronta): $URL/releases/tag/$TAG"
