# DGFScarlett su GitHub: AppImage per Linux

*Designed by Diego Fantoma*

GitHub Actions prova DGFScarlett e costruisce l'AppImage su macchine Linux messe a disposizione da GitHub.
Carichi il progetto una volta; poi, per ogni nuova versione, basta `./release.sh`. Dopo 5–10 minuti la
pagina **Releases** contiene:

| File | Cos'è |
|---|---|
| `DGFScarlett-2.0.0-x86_64.AppImage` | il programma in un solo file, per Ubuntu/Xubuntu 22.04+, Debian 12+, Fedora 35+… |
| `DGFScarlett-2.0.0-x86_64.AppImage.sha256` | l'impronta per controllare il download |

L'AppImage contiene Python e Qt. Sul computer di destinazione servono solo `alsa-utils` (`amixer`,
`aseqdump`), installati quasi ovunque.

Prima di costruire, GitHub prova DGFScarlett con Python 3.10 (come Ubuntu 22.04) e 3.12:
- controllo statico del codice e degli script;
- test con la **scheda simulata** (`tests/fake_amixer.py`): lettura dei controlli, curva dei fader, MIDI,
  patch bay, mute, vumeter, finestra;
- `--self-test`, eseguito anche sull'AppImage finita.

Se qualcosa non va, la Release non riceve il file e il motivo si legge nella scheda **Actions**.
DGFScarlett esiste solo per Linux (usa ALSA), quindi niente Windows/macOS: ogni versione consuma pochi minuti.

---

## 1. Caricare il progetto (una volta sola)

Servono `git` e `gh` collegato al tuo account, come per CueStage e Script Follower:

```bash
sudo apt install git gh          # se non li hai già
gh auth login                    # se non l'hai già fatto: GitHub.com › HTTPS › Login with a web browser
```

Poi, dalla cartella del progetto:

```bash
./crea_repo_github.sh            # pubblico, nome «DGFScarlett»
./crea_repo_github.sh --privato  # oppure privato
```

Lo script salva le modifiche in un commit, crea `https://github.com/<tuo-utente>/DGFScarlett` e ci carica il
ramo `main`. Nella scheda **Actions** parte subito il primo «Build»: sono solo i test e durano 2–3 minuti.
Il segno verde ✔ vuol dire che sono riusciti.

Se il progetto è già collegato a GitHub, lo script si ferma e ti indica `./release.sh`.

**Pubblico o privato?**
- Con il repository **pubblico** GitHub Actions è gratuito senza limiti.
- Con il repository **privato** il piano Free comprende 2000 minuti al mese. Una versione completa ne usa
  circa 10, quindi bastano comunque.

## 2. Pubblicare una versione (ogni volta)

```bash
./release.sh               # chiede il numero (propone il successivo, es. 2.0.1)
./release.sh 2.1.0         # oppure lo indichi tu
./release.sh --prova       # mostra cosa farebbe, senza cambiare né inviare nulla
```

Lo script:
1. controlla git, il collegamento a GitHub e che la versione sia nuova (se su GitHub ci sono commit che qui
   mancano, ti chiede prima un `git pull`);
2. chiede una breve descrizione delle novità e mostra il riepilogo;
3. esegue i test in locale con la scheda simulata, se vuoi (circa 10 secondi);
4. scrive la versione in `dgfscarlett.py` (`VERSION = "…"`), fa il commit e lo invia;
5. crea il tag `vX.Y.Z` e lo invia: GitHub Actions prova, costruisce l'AppImage e la allega alla Release;
6. se c'è `gh`, ti propone di seguire la costruzione e alla fine mostra il link alla Release.

### A mano, senza lo script

```bash
# in dgfscarlett.py:  VERSION = "2.0.1"
git add -A && git commit -m "Versione 2.0.1" && git push
git tag v2.0.1 && git push origin v2.0.1
```

Il tag deve corrispondere a `VERSION` in `dgfscarlett.py`, altrimenti il lavoro si ferma con un messaggio
chiaro. Per riprovare dopo una correzione:

```bash
git tag -d v2.0.1 && git push origin :refs/tags/v2.0.1
./release.sh 2.0.1
```

### Prova senza pubblicare

GitHub › **Actions** › **Build** (a sinistra) › **Run workflow** › *Run workflow*: costruisce l'AppImage senza
creare una Release. Il file resta 3 giorni in fondo alla pagina del lavoro, sotto **Artifacts**.

## 3. Quando qualcosa va storto

- **Actions** › il lavoro con la ✘ rossa › il passaggio con la ✘ › leggi le ultime righe del registro.
- Il **self-test** stampa una riga ✔ / ! / ✘ per ogni parte (lettura ALSA, PySide6, amixer, aseqdump).
- Mandami il testo dell'errore: di solito è una correzione di una riga.

## 4. Usare l'AppImage

```bash
chmod +x DGFScarlett-2.0.0-x86_64.AppImage
./DGFScarlett-2.0.0-x86_64.AppImage                  # finestra
./DGFScarlett-2.0.0-x86_64.AppImage meters           # gli stessi comandi del programma
```

Se all'avvio dice che manca FUSE (Ubuntu 22.04+: `sudo apt install libfuse2`), oppure senza installare nulla:
`./DGFScarlett-2.0.0-x86_64.AppImage --appimage-extract-and-run`.

## 5. Costruire l'AppImage a mano (senza GitHub)

Dalla cartella del progetto, su Ubuntu/Debian x86_64 con la connessione a Internet:

```bash
packaging/build_appimage.sh
```

Non servono permessi di root. Il file finisce nella cartella `dist/`; i download restano in `build/cache`
per le volte successive.
