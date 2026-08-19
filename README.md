# cryptdrive

Sichert einen lokalen Ordner komprimiert und verschlüsselt in ein zweites,
lokal gemountetes Verzeichnis (z. B. Google Drive) und hält dabei eine
versionierte Historie, in der man jederzeit auf einen früheren Stand
zurückspringen kann.

Wichtigste Eigenschaften:

* **Inkrementell**: pro Datei ein inhaltsadressiertes Objekt. Unveränderte
  Dateien kosten keinen Upload, identische Inhalte werden dedupliziert.
* **Reversibel**: jeder Lauf schreibt einen Snapshot (Manifest). Restore auf
  ein beliebiges Datum, auch für gelöschte Dateien.
* **Adaptive Konsolidierung**: erst wenn das Archiv sein Größenlimit
  überschreitet, werden die *ältesten großen* Änderungen zusammengefasst.
  Löschungen werden dabei nie angetastet.
* **Kompression mit Verstand**: zstd Level 19, aber nicht für Dateitypen, die
  schon komprimiert sind (mp4, jpg, zip, ...); bei großen Dateien entscheidet
  zusätzlich eine Stichprobe.
* **Verschlüsselung**: AES-256-GCM je Objekt, Schlüssel per Argon2id aus einem
  Passwort ableitbar. Der lokale Ordner bleibt unverschlüsselt.
* **Ignore-Regeln**: alle `.gitignore`-Dateien im Quellordner werden beachtet
  (auch mehrere Repos, Negationen, tiefere Regeln gewinnen), plus eine zentrale
  Ignore-Datei für temporäre Daten.
* **Hintergrundbetrieb**: täglicher Lauf um 03:00, verpasste Läufe werden beim
  nächsten Start nachgeholt. Taskleistensymbol für Status und manuellen Lauf,
  kleine GUI für die Wiederherstellung.

## Installation (Windows)

```powershell
.\install\install.ps1 -Source "C:\Users\stefa\Documents" -Archive "G:\Meine Ablage\cryptdrive"
```

Das Skript legt `.venv` an, installiert die Abhängigkeiten, fragt das Passwort
ab, richtet Archiv und Konfiguration ein, trägt den Dienst in den Autostart ein
(`HKCU\...\Run`), erstellt einen Startmenü-Eintrag für die Restore-GUI und
startet das Taskleistensymbol.

Deinstallation: `.\install\uninstall.ps1` (optional `-RemoveState`, `-RemoveArchive`).

Manuell geht es auch:

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m cryptdrive init --source <Ordner> --archive <Archivordner>
.venv/Scripts/python -m cryptdrive daemon
```

## Bedienung

Nach der Installation steht `cryptdrive.cmd` im Projektordner bereit:

```bash
cryptdrive status                        # Größen, letzter Lauf, Zustand
cryptdrive sync                          # sofort synchronisieren
cryptdrive snapshots                     # alle Stände auflisten
cryptdrive restore --at "2026-07-01" --dest D:\wiederherstellung
cryptdrive restore-gui                   # GUI mit Datumsauswahl
cryptdrive consolidate --dry-run         # Vorschau der Konsolidierung
cryptdrive verify                        # alle Objekte entschlüsseln und prüfen
cryptdrive scan --list                   # zeigt, was archiviert würde
cryptdrive unlock                        # Schlüsseldatei aus dem Passwort neu erzeugen
cryptdrive autostart --disable           # Autostart abschalten
```

Das Taskleistensymbol zeigt Ordnergröße (unkomprimiert), Archivgröße (mit
Historie), Anzahl der Snapshots, letzten und nächsten Lauf sowie während eines
Laufs Phase und Fortschritt. Über das Menü lassen sich ein Lauf starten, die
Restore-GUI öffnen, Log und Konfiguration anzeigen.

## Wie die Historie funktioniert

Ein Lauf schreibt ein Manifest: Pfad, Inhalts-Hash, Größe, Zeitstempel. Der
Inhalt selbst liegt einmal als Objekt unter `objects/`. Eine Datei ist gelöscht,
wenn sie im neueren Manifest fehlt; ihr letztes Objekt bleibt erhalten, solange
irgendein Snapshot es referenziert.

Überschreitet das Archiv `history.max_archive_size`, läuft die Konsolidierung:

1. Alle Versionen bestimmen (Läufe aufeinanderfolgender Snapshots mit gleichem
   Hash).
2. Kandidat ist eine Version, die von einer **neueren Version derselben Datei**
   abgelöst wurde und mindestens `history.min_change_size` (Default 100 MiB) im
   Archiv belegt. Wurde eine Datei stattdessen **gelöscht**, ist ihre letzte
   Version geschützt und wird nie konsolidiert.
3. Kandidaten werden von alt nach neu abgearbeitet, bis
   `target_ratio * max_archive_size` (Default 90 %) erreicht ist.
4. Konsolidieren heißt: in den alten Snapshots zeigt der Pfad auf die neuere
   Fassung (im Manifest als `cons` markiert), das alte Objekt wird gelöscht.
   Der Zeitpunkt bleibt also rekonstruierbar, nur diese Zwischenfassung ist weg.
5. Findet sich nichts oberhalb der Schwelle, wird sie halbiert, bis
   `min_change_size_floor` (Default 8 MiB) erreicht ist. Löschungen bleiben in
   jedem Fall geschützt.

Damit sparen wenige Eingriffe viel Platz: die dicken, oft geänderten Dateien
tragen die Historie, kleine Textänderungen bleiben vollständig erhalten.

Grenze: das Archiv kann nie kleiner werden als der aktuelle Stand, weil die
neueste Fassung jeder Datei immer erhalten bleibt. Ist schon der aktuelle Stand
größer als das Limit, meldet das Tool das (Statusanzeige und Log) und arbeitet
normal weiter.

## Archivformat

```
archive.json                 KDF-Salt, Argon2-Parameter, Schlüssel-Verifier (Klartext)
objects/ab/cd/<hash>.blob    zstd-komprimiert, AES-256-GCM verschlüsselt
snapshots/<id>.snap          verschlüsseltes Manifest je Lauf (UTC-Zeitstempel als ID)
tmp/                         Zwischendateien, werden bei jedem Lauf aufgeräumt
```

Der Objektname ist ein *keyed* BLAKE2b-Hash des Klartexts: ohne Schlüssel
verrät das Archiv weder Inhalte noch Dateinamen, nur Anzahl und Größen. Jedes
Objekt bekommt über HKDF einen eigenen Schlüssel (Salt im Header), Daten werden
in 4-MiB-Segmenten mit AES-256-GCM authentifiziert. Das Endsegment ist markiert,
damit auch Abschneiden auffällt. Beim Restore wird zusätzlich der Hash geprüft.

## Wiederherstellung auf einem fremden Rechner

Es genügen Archivordner und Passwort:

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m cryptdrive restore-gui
```

In der GUI "Schlüssel aus Passwort ableiten" wählen, Archivordner angeben,
Snapshots laden, Datum wählen, Zielordner setzen, starten. Alternativ per CLI
`init` mit dem bestehenden Archiv (erkennt es und leitet nur den Schlüssel ab)
und dann `restore`.

## Konfiguration

`%LOCALAPPDATA%\cryptdrive\config.toml`, Beispiel siehe
[config.example.toml](config.example.toml). Wichtige Werte:

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `source`, `archive` | | Quell- und Archivordner |
| `schedule.daily_time` | `03:00` | Uhrzeit des täglichen Laufs |
| `schedule.catch_up` | `true` | verpassten Lauf beim Start nachholen |
| `compression.level` | `19` | zstd-Level |
| `compression.skip_extensions` | mp4, jpg, zip, ... | keine Kompression versuchen |
| `compression.probe_min_ratio` | `0.95` | Stichprobe: schlechter heißt unkomprimiert |
| `history.max_archive_size` | `100 GiB` | ab hier wird konsolidiert |
| `history.min_change_size` | `100 MiB` | Mindestgröße einer Änderung |
| `history.protect_deletions` | `true` | Löschungen nie konsolidieren |
| `ignore.include_git_dirs` | `true` | `.git`-Ordner mitsichern |
| `ignore.max_file_size` | `0` | Dateien darüber überspringen (0 = alle) |
| `crypto.argon2_*` | 4 / 256 MiB / 4 | KDF-Kosten (nur bei `init` relevant) |

Die zentrale Ignore-Datei liegt unter `%LOCALAPPDATA%\cryptdrive\ignore.conf`
und nutzt `.gitignore`-Syntax. Sie wird beim ersten Lauf mit sinnvollen
Vorgaben angelegt (Caches, Build-Ordner, temporäre Dateien, OS-Müll).

## Dateien

| Datei | Inhalt |
|---|---|
| [cryptdrive/crypto.py](cryptdrive/crypto.py) | Schlüsselableitung, Kompression, AES-GCM-Blobformat |
| [cryptdrive/archive.py](cryptdrive/archive.py) | Blob-Store, Snapshots, GC |
| [cryptdrive/scanner.py](cryptdrive/scanner.py) | Walk mit `.gitignore`-Auswertung |
| [cryptdrive/sync.py](cryptdrive/sync.py) | Sync-Ablauf und Fortschritt |
| [cryptdrive/consolidate.py](cryptdrive/consolidate.py) | adaptive Konsolidierung |
| [cryptdrive/restore.py](cryptdrive/restore.py) | Restore und Verify |
| [cryptdrive/daemon.py](cryptdrive/daemon.py) | Zeitplan, Zustand, Logging |
| [cryptdrive/tray.py](cryptdrive/tray.py) | Taskleistensymbol |
| [cryptdrive/restore_gui.py](cryptdrive/restore_gui.py) | GUI für die Wiederherstellung |
| [cryptdrive/cli.py](cryptdrive/cli.py) | Kommandozeile |

## Tests

```bash
.venv/Scripts/python tests/test_roundtrip.py
```

Der Test baut in einem temporären Ordner ein Beispielprojekt mit Git-Repo,
ignorierten Dateien, großen Binärdateien und `.git`-Verzeichnis, sichert es,
ändert und löscht Dateien, stellt alte Stände wieder her und prüft die
Konsolidierung inklusive Schutz von Löschungen. Er verwendet ein eigenes
`CRYPTDRIVE_HOME` und lässt die echte Installation unberührt.

## Hinweise und Grenzen

* Symlinks und Junctions werden übersprungen (kein Folgen, keine Archivierung).
* Der Cloud-Client muss die Dateien selbst hochladen. cryptdrive schreibt nur in
  den gemounteten Ordner und prüft nicht, ob der Upload durch ist.
* Der lokale Schlüssel liegt in `%LOCALAPPDATA%\cryptdrive\master.key`, per
  `icacls` auf den eigenen Benutzer beschränkt. Ohne Passwort **und** ohne diese
  Datei ist das Archiv nicht wiederherstellbar: Passwort physisch sichern.
* Zeitstempel der Snapshots sind UTC, die Anzeige erfolgt in lokaler Zeit.
