"""Ende-zu-Ende-Test von cryptdrive.

Aufruf:  .venv/Scripts/python tests/test_roundtrip.py

Baut einen Beispielordner samt Git-Repo in einem temporaeren Verzeichnis,
sichert ihn, aendert und loescht Dateien, stellt alte Staende wieder her
und prueft die adaptive Konsolidierung. Das echte System bleibt unberuehrt:
der Zustandsordner wird ueber CRYPTDRIVE_HOME umgebogen.
"""
import os
import shutil
import tempfile
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BASE = Path(tempfile.mkdtemp(prefix="cryptdrive-test-"))
SRC = BASE / "src"
ARCH = BASE / "drive"
HOME = BASE / "home"
for p in (SRC, ARCH, HOME):
    p.mkdir(parents=True)
os.environ["CRYPTDRIVE_HOME"] = str(HOME)


from cryptdrive import crypto  # noqa: E402
from cryptdrive.archive import Archive  # noqa: E402
from cryptdrive.cli import main as cli  # noqa: E402
from cryptdrive.config import load_config  # noqa: E402
from cryptdrive.consolidate import analyse, consolidate, plan  # noqa: E402
from cryptdrive.restore import list_snapshots, restore  # noqa: E402
from cryptdrive.sync import run_sync  # noqa: E402
from cryptdrive.util import fmt_size  # noqa: E402

PW = "korrekt-pferd-batterie-heftklammer"
CFG = HOME / "config.toml"
fails = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)


def write(rel, data, size=None, random=False):
    p = SRC / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if size:
        with open(p, "wb") as fh:
            written = 0
            while written < size:
                block = os.urandom(65536) if random else (data * 512)[:4096].encode()
                fh.write(block)
                written += len(block)
    else:
        p.write_text(data, encoding="utf-8")
    return p


print("== 1. Quellordner aufbauen ==")
write("notizen.txt", "hallo welt")
write("repo/.gitignore", "build/\n*.log\n!wichtig.log\n")
write("repo/main.py", "print('hi')\n")
write("repo/build/artifact.bin", "muell")
write("repo/debug.log", "rauschen")
write("repo/wichtig.log", "bitte sichern")
write("repo/nested/.gitignore", "!*.log\n")
write("repo/nested/trotzdem.log", "wieder eingeschlossen")
write("temp/cache.tmp", "wegwerf")
write("gross/daten.bin", "A", size=6 * 1024 * 1024, random=True)
(SRC / "repo" / ".git").mkdir()
write("repo/.git/HEAD", "ref: refs/heads/main\n")

shutil.copyfile(SRC / "gross" / "daten.bin", BASE / "keep_v1.bin")

print("== 2. init ==")
rc = cli(["--config", str(CFG), "init", "--source", str(SRC), "--archive", str(ARCH),
          "--password", PW, "--max-archive-size", "8 MiB", "--min-change-size", "2 MiB",
          "--compression-level", "10"])
check(rc == 0, "init rc=0")
cfg = load_config(CFG)
check(cfg.key_file.exists(), "Schluesseldatei angelegt")
keyring = crypto.load_key_file(cfg.key_file)
check(crypto.keyring_from_password(ARCH, PW).master == keyring.master,
      "Key aus Passwort rekonstruierbar")
try:
    crypto.keyring_from_password(ARCH, "falsch")
    check(False, "falsches Passwort wird abgelehnt")
except crypto.CryptoError:
    check(True, "falsches Passwort wird abgelehnt")

print("== 3. erster Sync ==")
r1 = run_sync(cfg, keyring)
paths = set(Archive(ARCH, keyring, cfg.compression).load_snapshot(r1.snapshot_id).files)
print("   ", sorted(paths))
check("notizen.txt" in paths, "normale Datei archiviert")
check("repo/build/artifact.bin" not in paths, "gitignore: build/ ausgeschlossen")
check("repo/debug.log" not in paths, "gitignore: *.log ausgeschlossen")
check("repo/wichtig.log" in paths, "gitignore: Negation !wichtig.log greift")
check("repo/nested/trotzdem.log" in paths, "tiefere .gitignore gewinnt")
check("temp/cache.tmp" not in paths, "zentrale ignore-Datei: *.tmp ausgeschlossen")
check("repo/.git/HEAD" in paths, ".git wird mitgesichert")
check(r1.added == len(paths) and r1.modified == 0, "erster Lauf: alles neu")
check(r1.uploaded_blobs == len({e['h'] for e in
      Archive(ARCH, keyring, cfg.compression).load_snapshot(r1.snapshot_id).files.values()}),
      "je Inhalt genau ein Objekt")

print("== 4. inkrementeller Sync ohne Aenderung ==")
r2 = run_sync(cfg, keyring)
check(r2.uploaded_blobs == 0, "keine Uploads ohne Aenderung")
check(r2.added == 0 and r2.modified == 0 and r2.deleted == 0, "keine Aenderungen erkannt")

print("== 5. Aenderung + Loeschung ==")
time.sleep(1.1)
write("notizen.txt", "hallo welt, geaendert")
write("gross/daten.bin", "B", size=6 * 1024 * 1024, random=True)
(SRC / "repo" / "main.py").unlink()
r3 = run_sync(cfg, keyring, consolidation=False)   # Konsolidierung hier bewusst aus
check(r3.modified == 2, f"2 Aenderungen erkannt (war {r3.modified})")
check(r3.deleted == 1, "1 Loeschung erkannt")
check(r3.uploaded_blobs == 2, f"2 Objekte hochgeladen (war {r3.uploaded_blobs})")

print("== 6. Historie: alten Stand wiederherstellen ==")
dest = BASE / "restore1"
res = restore(cfg, keyring, when=r1.snapshot_id, dest=dest)
check((dest / "notizen.txt").read_text(encoding="utf-8") == "hallo welt",
      "alte Fassung wiederhergestellt")
check((dest / "repo" / "main.py").exists(), "geloeschte Datei aus Historie zurueck")
check((dest / "gross" / "daten.bin").read_bytes() ==
      (BASE / "keep_v1.bin").read_bytes(), "grosse Binaerdatei bytegleich zurueck")
check(res.errors == [], "Restore ohne Fehler")

print("== 7. Kompressionsentscheidung ==")
from cryptdrive.crypto import should_compress  # noqa: E402
write("medien/clip.mp4", "X", size=1024 * 1024)
check(should_compress(SRC / "medien" / "clip.mp4", 1024 * 1024, cfg.compression) is False,
      "mp4 wird nicht komprimiert")
check(should_compress(SRC / "notizen.txt", 20, cfg.compression) is True,
      "Textdatei wird komprimiert")
rand = SRC / "medien" / "rauschen.dat"
rand.parent.mkdir(parents=True, exist_ok=True)
rand.write_bytes(os.urandom(5 * 1024 * 1024))
check(should_compress(rand, 5 * 1024 * 1024, cfg.compression) is False,
      "inkompressible Grossdatei per Stichprobe erkannt")

print("== 8. Konsolidierung ==")
archive = Archive(ARCH, keyring, cfg.compression)
cands, stats = analyse(archive)
print("    Kandidaten:", [(c.path, fmt_size(c.size)) for c in cands],
      "geschuetzte Loeschungen:", stats["protected_deletions"])
check(any(c.path == "gross/daten.bin" for c in cands), "grosse Aenderung ist Kandidat")
check(not any(c.path == "repo/main.py" for c in cands), "Loeschung ist kein Kandidat")
check(stats["protected_deletions"] >= 1, "Loeschung als geschuetzt gezaehlt")
size_before = archive.total_size()
rep = consolidate(cfg, archive, progress_cb=lambda m: print("    " + m))
check(rep["ran"], "Konsolidierung ausgefuehrt")
check(rep["after"] < size_before, f"Archiv geschrumpft: {fmt_size(size_before)} -> "
      f"{fmt_size(rep['after'])}")
snap1 = archive.load_snapshot(r1.snapshot_id)
check(snap1.files["gross/daten.bin"].get("cons") is True,
      "alter Snapshot zeigt jetzt auf neuere Fassung")
check("repo/main.py" in snap1.files, "geloeschte Datei bleibt in der Historie")
dest2 = BASE / "restore2"
res2 = restore(cfg, keyring, when=r1.snapshot_id, dest=dest2)
check(res2.errors == [], "Restore nach Konsolidierung fehlerfrei")
check((dest2 / "repo" / "main.py").exists(), "geloeschte Datei weiter rekonstruierbar")
check((dest2 / "gross" / "daten.bin").read_bytes() ==
      (SRC / "gross" / "daten.bin").read_bytes(),
      "konsolidierte Datei liefert die neuere Fassung")

print("== 8b. Konsolidierung laeuft automatisch im Sync ==")
time.sleep(1.1)
write("gross/daten.bin", "C", size=6 * 1024 * 1024, random=True)
r4 = run_sync(cfg, keyring)
check(r4.consolidated.get("ran") is True, "Sync hat automatisch konsolidiert")
check(r4.consolidated.get("bytes_freed", 0) > 5 * 1024 * 1024,
      f"dabei {fmt_size(r4.consolidated.get('bytes_freed', 0))} freigegeben")
# Das Limit kann nie kleiner werden als der aktuelle Stand selbst: die
# neueste Fassung jeder Datei bleibt immer erhalten.
live = sum(int(e["c"]) for e in
           Archive(ARCH, keyring, cfg.compression).load_snapshot(
               r4.snapshot_id).files.values())
check(r4.archive_bytes <= live * 1.05 + 65536,
      f"Archiv auf den aktuellen Stand eingeschrumpft ({fmt_size(r4.archive_bytes)}, "
      f"aktueller Stand {fmt_size(live)})")
check(r4.consolidated.get("still_over") is True,
      "Ueberschreitung wird gemeldet, wenn schon der aktuelle Stand zu gross ist")
archive2 = Archive(ARCH, keyring, cfg.compression)
check("repo/main.py" in archive2.load_snapshot(r1.snapshot_id).files,
      "Loeschung bleibt auch nach mehreren Runden geschuetzt")
orphans = [h for h, _ in archive2.iter_blobs() if h not in archive2.referenced_hashes()]
check(not orphans, "keine verwaisten Objekte im Archiv")

print("== 9. verify + CLI ==")
from cryptdrive.restore import verify_archive  # noqa: E402
rep = verify_archive(cfg, keyring)
check(not rep["missing"] and not rep["broken"], "verify: alle Objekte intakt")
check(cli(["--config", str(CFG), "snapshots"]) == 0, "CLI snapshots")
check(cli(["--config", str(CFG), "status"]) == 0, "CLI status")
check(cli(["--config", str(CFG), "consolidate", "--dry-run"]) == 0, "CLI consolidate --dry-run")
check(cli(["--config", str(CFG), "scan"]) == 0, "CLI scan")

print("== 10. Manipulation am Archiv wird erkannt ==")
snap = archive.load_snapshot(r1.snapshot_id)
digest = snap.files["notizen.txt"]["h"]
blob = archive.blob_path(digest)
raw = bytearray(blob.read_bytes())
raw[-1] ^= 0xFF
blob.write_bytes(bytes(raw))
try:
    archive.verify_blob(digest)
    check(False, "manipulierter Blob wird abgelehnt")
except crypto.CryptoError:
    check(True, "manipulierter Blob wird abgelehnt")

print("== 11. Zeitplan ==")
from datetime import datetime, time as dtime, timedelta  # noqa: E402
from cryptdrive.daemon import most_recent_due, next_due, parse_daily_time  # noqa: E402
daily = parse_daily_time("03:00")
now = datetime(2026, 8, 19, 10, 30).astimezone()
check(most_recent_due(now, daily) == now.replace(hour=3, minute=0, second=0, microsecond=0),
      "letzter faelliger Termin heute 03:00")
check(next_due(now, daily) == now.replace(hour=3, minute=0, second=0, microsecond=0)
      + timedelta(days=1), "naechster Termin morgen 03:00")
early = datetime(2026, 8, 19, 1, 15).astimezone()
check(most_recent_due(early, daily).day == 18, "vor 03:00 ist der Termin von gestern faellig")

print()
if fails:
    print(f"{len(fails)} FEHLER:")
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("Alle Tests bestanden.")
shutil.rmtree(BASE, ignore_errors=True)
