"""Kommandozeile von cryptdrive."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import secrets
import sys
from datetime import datetime
from pathlib import Path

from . import crypto
from .archive import Archive
from .config import (Config, CryptoCfg, HistoryCfg, default_config_path,
                     ensure_ignore_file, load_config, save_config, state_dir)
from .crypto import Keyring
from .util import fmt_local, fmt_size, parse_size

log = logging.getLogger("cryptdrive.cli")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "cryptdrive"


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def get_keyring(cfg: Config, password: str | None = None,
                prompt: bool = True) -> Keyring:
    """Schluessel laden: lokale Keydatei bevorzugt, sonst aus dem Passwort."""
    if password:
        return crypto.keyring_from_password(cfg.archive_path, password)
    if cfg.key_file.exists():
        keyring = crypto.load_key_file(cfg.key_file)
        try:
            meta = crypto.read_archive_meta(cfg.archive_path)
            if meta.get("verifier") and keyring.verifier != meta["verifier"]:
                raise crypto.CryptoError(
                    "Lokaler Schluessel passt nicht zum Archiv. "
                    "Mit 'cryptdrive unlock' neu aus dem Passwort ableiten.")
        except crypto.CryptoError:
            raise
        return keyring
    if not prompt:
        raise FileNotFoundError(f"Kein Schluessel unter {cfg.key_file}.")
    return crypto.keyring_from_password(cfg.archive_path, getpass.getpass("Passwort: "))


def _pythonw() -> str:
    exe = Path(sys.executable)
    if os.name == "nt" and exe.name.lower() == "python.exe":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def autostart_command(cfg: Config) -> str:
    return f'"{_pythonw()}" -m cryptdrive daemon --config "{cfg.path}"'


# --------------------------------------------------------------------------
# Befehle
# --------------------------------------------------------------------------

def cmd_init(args) -> int:
    cfg_path = Path(args.config) if args.config else default_config_path()
    existing = None
    if cfg_path.exists() and not args.force:
        try:
            existing = load_config(cfg_path)
            print(f"Konfiguration existiert bereits: {cfg_path}")
        except Exception:
            existing = None

    source = args.source or (existing.source if existing else "") or \
        input("Quellordner (wird gesichert): ").strip().strip('"')
    archive = args.archive or (existing.archive if existing else "") or \
        input("Archivordner (z. B. im gemounteten Google Drive): ").strip().strip('"')
    source_path = Path(source).expanduser()
    if not source_path.is_dir():
        print(f"Quellordner nicht gefunden: {source_path}", file=sys.stderr)
        return 2
    archive_path = Path(archive).expanduser()
    archive_path.mkdir(parents=True, exist_ok=True)

    cfg = existing or Config()
    cfg.source = str(source_path)
    cfg.archive = str(archive_path)
    cfg.path = cfg_path
    if args.max_archive_size:
        cfg.history.max_archive_size = args.max_archive_size
    if args.min_change_size:
        cfg.history.min_change_size = args.min_change_size
    if args.daily_time:
        cfg.schedule.daily_time = args.daily_time
    if args.compression_level:
        cfg.compression.level = int(args.compression_level)

    already = (archive_path / crypto.ARCHIVE_META).exists()
    password = args.password
    if not password:
        if already:
            password = getpass.getpass("Passwort des bestehenden Archivs: ")
        else:
            password = getpass.getpass("Neues Passwort (bitte physisch sichern): ")
            again = getpass.getpass("Passwort wiederholen: ")
            if password != again:
                print("Passwoerter stimmen nicht ueberein.", file=sys.stderr)
                return 2
            if len(password) < 10:
                print("Warnung: Passwort ist kurz. Empfohlen sind >= 16 Zeichen.")

    if already:
        keyring = crypto.keyring_from_password(archive_path, password)
        print("Bestehendes Archiv erkannt, Schluessel aus Passwort abgeleitet.")
    else:
        salt = secrets.token_bytes(16)
        print("Leite Schluessel ab (Argon2id, dauert einen Moment) ...")
        master = crypto.derive_master_key(
            password, salt, cfg.crypto.argon2_time_cost,
            cfg.crypto.argon2_memory_kib, cfg.crypto.argon2_parallelism)
        keyring = Keyring(master)
        crypto.write_archive_meta(archive_path, salt, keyring, cfg.crypto)
        print(f"Archiv initialisiert: {archive_path}")

    crypto.save_key_file(cfg.key_file, keyring.master)
    Archive(archive_path, keyring, cfg.compression).ensure_layout()
    save_config(cfg, cfg_path)
    ignore = ensure_ignore_file(cfg)
    print(f"Konfiguration:  {cfg_path}")
    print(f"Schluesseldatei: {cfg.key_file}")
    print(f"Ignore-Datei:    {ignore}")
    print(f"Limit Archiv:    {fmt_size(cfg.max_archive_bytes)}, "
          f"Konsolidierung ab {fmt_size(cfg.min_change_bytes)} pro Aenderung")
    print(f"Taeglicher Lauf: {cfg.schedule.daily_time} (verpasste Laeufe werden nachgeholt)")
    return 0


def cmd_sync(args) -> int:
    from .daemon import setup_logging
    from .sync import run_sync
    from .util import SingleInstanceLock
    cfg = load_config(args.config)
    setup_logging(cfg, to_console=args.verbose)
    keyring = get_keyring(cfg, args.password)

    # Verhindert, dass diese manuelle Sync mit dem Hintergrunddienst (der
    # z. B. gerade einen verpassten Lauf nachholt) um dieselbe SQLite-Datei
    # konkurriert. Ohne dieses Lock kollidieren beide Prozesse mit
    # "database is locked".
    lock = SingleInstanceLock(cfg.lock_file)
    if not lock.acquire():
        print("Ein anderer cryptdrive-Prozess synchronisiert bereits "
              "(z. B. der Hintergrunddienst). Bitte 'cryptdrive status' "
              "abwarten oder spaeter erneut versuchen.", file=sys.stderr)
        return 3

    last = [0.0]

    def on_progress(p):
        import time
        if p.phase in ("done", "snapshot") or time.monotonic() - last[0] > 1.0:
            last[0] = time.monotonic()
            print(f"\r{p.phase:12s} {p.percent:5.1f} %  "
                  f"{p.files_done}/{p.files_total}  {p.current[-50:]:<50s}",
                  end="", flush=True)

    try:
        result = run_sync(cfg, keyring, progress_cb=on_progress if not args.quiet else None,
                          consolidation=not args.no_consolidate)
    finally:
        lock.release()
    print("\r" + " " * 100, end="\r")
    print(result.summary())
    if result.consolidated.get("ran"):
        c = result.consolidated
        print(f"Konsolidierung: {c['selected_count']} Aenderungen, "
              f"{fmt_size(c['bytes_freed'])} freigegeben, "
              f"Schwelle {fmt_size(c['threshold'])}")
    if result.errors:
        print(f"{len(result.errors)} Warnung(en), Details im Log: {cfg.log_file}")
    return 0


def cmd_status(args) -> int:
    from .state import LocalIndex, read_status
    cfg = load_config(args.config)
    status = read_status(cfg.status_file)
    with LocalIndex(cfg.index_db) as index:
        last = index.last_sync
        source_bytes = int(index.get("source_bytes", 0) or 0)
        source_files = int(index.get("source_files", 0) or 0)
        archive_bytes = int(index.get("archive_bytes", 0) or 0)
        snaps = int(index.get("snapshot_count", 0) or 0)
    if args.json:
        print(json.dumps({
            "state": status.get("state", "unknown"),
            "last_sync": last.isoformat() if last else None,
            "source_bytes": source_bytes, "source_files": source_files,
            "archive_bytes": archive_bytes, "snapshot_count": snaps,
            "next_run": status.get("next_run"),
        }, indent=2))
        return 0
    print(f"Quelle:   {cfg.source_path}")
    print(f"Archiv:   {cfg.archive_path}")
    print(f"Zustand:  {status.get('state', 'unbekannt')}"
          + (f" ({status.get('percent', 0)} %)" if status.get("state") == "syncing" else ""))
    print(f"Ordner:   {fmt_size(source_bytes)} in {source_files} Dateien (unkomprimiert)")
    print(f"Archiv:   {fmt_size(archive_bytes)} mit Historie, {snaps} Snapshots")
    if source_bytes and archive_bytes:
        print(f"Faktor:   {archive_bytes / source_bytes:.2f}x der Ordnergroesse")
    print(f"Letzter Sync: {fmt_local(last)}")
    if status.get("next_run"):
        print(f"Naechster Lauf: {fmt_local(datetime.fromisoformat(status['next_run']))}")
    if status.get("last_error"):
        print(f"Hinweis: {status['last_error']}")
    return 0


def cmd_snapshots(args) -> int:
    from .restore import list_snapshots
    cfg = load_config(args.config)
    keyring = get_keyring(cfg, args.password)
    archive = Archive(cfg.archive_path, keyring, cfg.compression)
    infos = list_snapshots(archive)
    if not infos:
        print("Noch keine Snapshots vorhanden.")
        return 0
    print(f"{'ID':18s} {'Stand vom':17s} {'Dateien':>8s} {'Inhalt':>12s} "
          f"{'Archivanteil':>13s} {'kons.':>6s}")
    for info in infos:
        print(f"{info.id:18s} {info.created.astimezone():%Y-%m-%d %H:%M}  "
              f"{info.files:8d} {fmt_size(info.source_bytes):>12s} "
              f"{fmt_size(info.stored_bytes):>13s} {info.consolidated:6d}")
    print(f"\nArchiv insgesamt: {fmt_size(archive.total_size())}")
    return 0


def cmd_restore(args) -> int:
    from .daemon import setup_logging
    from .restore import restore
    cfg = load_config(args.config)
    setup_logging(cfg, to_console=args.verbose)
    keyring = get_keyring(cfg, args.password)
    last = [0.0]

    def on_progress(p):
        import time
        if time.monotonic() - last[0] > 1.0:
            last[0] = time.monotonic()
            print(f"\r{p.percent:5.1f} %  {p.files_done}/{p.files_total}  "
                  f"{p.current[-50:]:<50s}", end="", flush=True)

    result = restore(cfg, keyring, when=args.at, dest=Path(args.dest) if args.dest else None,
                     subpaths=args.path or None, overwrite=args.overwrite,
                     progress_cb=on_progress)
    print("\r" + " " * 100, end="\r")
    print(f"Snapshot {result.snapshot_id}: {result.files} Dateien "
          f"({fmt_size(result.bytes_written)}), {result.skipped} uebersprungen, "
          f"{len(result.errors)} Fehler")
    for err in result.errors[:10]:
        print("  " + err)
    return 1 if result.errors else 0


def cmd_consolidate(args) -> int:
    from .consolidate import consolidate, plan
    from .daemon import setup_logging
    from .util import SingleInstanceLock
    cfg = load_config(args.config)
    setup_logging(cfg, to_console=args.verbose)
    if args.max_archive_size:
        cfg.history.max_archive_size = args.max_archive_size
    if args.min_change_size:
        cfg.history.min_change_size = args.min_change_size
    keyring = get_keyring(cfg, args.password)
    archive = Archive(cfg.archive_path, keyring, cfg.compression)
    if args.dry_run:
        info = plan(cfg, archive)
        print(f"Archiv {fmt_size(info['archive_bytes'])} / Limit {fmt_size(info['limit'])}"
              f" (Ziel {fmt_size(info['target'])})")
        print(f"Kandidaten insgesamt: {info['candidates']} "
              f"({fmt_size(info['candidate_bytes'])}), geschuetzte Loeschungen: "
              f"{info['protected_deletions']}")
        print(f"Auswahl bei Schwelle {fmt_size(info['threshold'])}: "
              f"{info['selected_count']} Aenderungen, "
              f"{fmt_size(info['would_free'])} wuerden frei")
        for line in info["selected"]:
            print("  " + line)
        return 0

    # Schreibt Snapshots und loescht Blobs, deshalb dasselbe Lock wie ein
    # Sync-Lauf: sonst koennte der Hintergrunddienst parallel dieselben
    # Objekte veraendern.
    lock = SingleInstanceLock(cfg.lock_file)
    if not lock.acquire():
        print("Ein anderer cryptdrive-Prozess ist gerade aktiv. Bitte abwarten.",
              file=sys.stderr)
        return 3
    try:
        report = consolidate(cfg, archive, progress_cb=lambda m: print(m), force=args.force)
    finally:
        lock.release()
    if not report["ran"]:
        print(f"Nichts zu tun. Archiv {fmt_size(report['before'])}, "
              f"Limit {fmt_size(report['limit'])}.")
        if report.get("still_over"):
            print("Achtung: Limit ueberschritten, aber kein zulaessiger Kandidat gefunden.")
        return 0
    print(f"{report['selected_count']} Aenderungen konsolidiert, "
          f"{fmt_size(report['bytes_freed'])} freigegeben "
          f"({fmt_size(report['before'])} -> {fmt_size(report['after'])})")
    return 0


def cmd_verify(args) -> int:
    from .restore import verify_archive
    cfg = load_config(args.config)
    keyring = get_keyring(cfg, args.password)
    report = verify_archive(cfg, keyring, snapshot=args.snapshot,
                            progress_cb=lambda m: print("\r" + m, end="", flush=True))
    print("\r" + " " * 60, end="\r")
    print(f"{report['checked']} Objekte geprueft ({fmt_size(report['bytes'])} Klartext)")
    if report["missing"]:
        print(f"FEHLEND: {len(report['missing'])} Objekte")
        for digest in report["missing"][:10]:
            print("  " + digest)
    if report["broken"]:
        print(f"DEFEKT: {len(report['broken'])} Objekte")
        for item in report["broken"][:10]:
            print("  " + item)
    return 1 if (report["missing"] or report["broken"]) else 0


def cmd_gc(args) -> int:
    from .util import SingleInstanceLock
    cfg = load_config(args.config)
    keyring = get_keyring(cfg, args.password)
    archive = Archive(cfg.archive_path, keyring, cfg.compression)
    lock = SingleInstanceLock(cfg.lock_file)
    if not lock.acquire():
        print("Ein anderer cryptdrive-Prozess ist gerade aktiv. Bitte abwarten.",
              file=sys.stderr)
        return 3
    try:
        count, freed = archive.gc()
        archive.clean_tmp()
    finally:
        lock.release()
    print(f"{count} nicht referenzierte Objekte entfernt, {fmt_size(freed)} frei")
    return 0


def cmd_unlock(args) -> int:
    """Lokale Schluesseldatei aus dem Passwort neu erzeugen."""
    cfg = load_config(args.config)
    password = args.password or getpass.getpass("Passwort: ")
    keyring = crypto.keyring_from_password(cfg.archive_path, password)
    crypto.save_key_file(cfg.key_file, keyring.master)
    print(f"Schluessel wiederhergestellt: {cfg.key_file}")
    return 0


def cmd_daemon(args) -> int:
    from .daemon import Daemon, setup_logging
    cfg = load_config(args.config)
    setup_logging(cfg, to_console=args.verbose)
    keyring = get_keyring(cfg, args.password, prompt=False)
    daemon = Daemon(cfg, keyring)
    lock = None
    if not args.allow_multiple:
        from .util import SingleInstanceLock
        lock = SingleInstanceLock(state_dir() / "daemon.lock")
        if not lock.acquire():
            print("cryptdrive laeuft bereits.", file=sys.stderr)
            return 1
    daemon.start()
    try:
        if args.no_tray:
            import time
            while True:
                time.sleep(3600)
        else:
            from .tray import run_tray
            run_tray(daemon)
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
        if lock:
            lock.release()
    return 0


def cmd_restore_gui(args) -> int:
    from .restore_gui import main as gui_main
    return gui_main(args.config)


def cmd_autostart(args) -> int:
    if os.name != "nt":
        print("Autostart wird derzeit nur unter Windows unterstuetzt.", file=sys.stderr)
        return 2
    import winreg
    cfg = load_config(args.config)
    command = autostart_command(cfg)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as key:
        if args.disable:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
                print("Autostart entfernt.")
            except FileNotFoundError:
                print("Autostart war nicht eingerichtet.")
        else:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
            print(f"Autostart eingerichtet:\n  {command}")
    return 0


def cmd_paths(args) -> int:
    cfg_path = Path(args.config) if args.config else default_config_path()
    print(f"Konfiguration: {cfg_path}")
    print(f"Zustandsordner: {state_dir()}")
    try:
        cfg = load_config(cfg_path)
    except Exception as exc:
        print(f"(Konfiguration nicht lesbar: {exc})")
        return 0
    print(f"Quelle:        {cfg.source_path}")
    print(f"Archiv:        {cfg.archive_path}")
    print(f"Schluessel:     {cfg.key_file}")
    print(f"Ignore-Datei:   {cfg.ignore_file}")
    print(f"Log:            {cfg.log_file}")
    print(f"Index:          {cfg.index_db}")
    return 0


def cmd_ignored(args) -> int:
    """Zeigen, was der Scan aktuell einsammeln wuerde (Diagnose)."""
    from .scanner import scan
    cfg = load_config(args.config)
    total = count = 0
    for info in scan(cfg):
        count += 1
        total += info.size
        if args.list:
            print(f"{fmt_size(info.size):>10s}  {info.rel}")
    print(f"{count} Dateien, {fmt_size(total)} werden archiviert "
          f"(ignore-Datei: {cfg.ignore_file})")
    return 0


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cryptdrive",
        description="Verschluesseltes, versioniertes Backup eines Ordners in ein "
                    "lokal gemountetes Cloud-Laufwerk.")
    p.add_argument("--config", help="Pfad zur Konfigurationsdatei")
    p.add_argument("-v", "--verbose", action="store_true", help="Logausgabe auf der Konsole")
    p.add_argument("--password", help="Passwort (sonst lokaler Schluessel oder Abfrage)")

    # Dieselben Optionen sollen auch nach dem Unterbefehl erlaubt sein.
    # SUPPRESS sorgt dafuer, dass ein globaler Wert nicht ueberschrieben wird.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--password", default=argparse.SUPPRESS)

    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("init", help="Konfiguration und Archiv einrichten", parents=[common])
    i.add_argument("--source")
    i.add_argument("--archive")
    i.add_argument("--max-archive-size", help="z. B. '200 GiB'")
    i.add_argument("--min-change-size", help="z. B. '100 MiB'")
    i.add_argument("--daily-time", help="z. B. '03:00'")
    i.add_argument("--compression-level", type=int)
    i.add_argument("--force", action="store_true", help="bestehende Konfiguration ersetzen")
    i.set_defaults(func=cmd_init)

    s = sub.add_parser("sync", help="Jetzt synchronisieren", parents=[common])
    s.add_argument("--no-consolidate", action="store_true")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_sync)

    st = sub.add_parser("status", help="Status anzeigen", parents=[common])
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    sn = sub.add_parser("snapshots", help="Alle Staende auflisten", parents=[common])
    sn.set_defaults(func=cmd_snapshots)

    r = sub.add_parser("restore", help="Stand wiederherstellen", parents=[common])
    r.add_argument("--at", help="Datum/Zeit oder Snapshot-ID (Default: neuester)")
    r.add_argument("--dest", help="Zielordner (Default: Quellordner)")
    r.add_argument("--path", action="append", help="nur diesen Unterpfad (mehrfach moeglich)")
    r.add_argument("--overwrite", action="store_true")
    r.set_defaults(func=cmd_restore)

    c = sub.add_parser("consolidate", help="Historie konsolidieren", parents=[common])
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--force", action="store_true", help="auch unterhalb des Limits")
    c.add_argument("--max-archive-size")
    c.add_argument("--min-change-size")
    c.set_defaults(func=cmd_consolidate)

    v = sub.add_parser("verify", help="Archiv entschluesseln und pruefen", parents=[common])
    v.add_argument("--snapshot", help="nur diesen Stand pruefen")
    v.set_defaults(func=cmd_verify)

    g = sub.add_parser("gc", help="nicht referenzierte Objekte entfernen", parents=[common])
    g.set_defaults(func=cmd_gc)

    u = sub.add_parser("unlock", help="lokale Schluesseldatei aus dem Passwort erzeugen", parents=[common])
    u.set_defaults(func=cmd_unlock)

    d = sub.add_parser("daemon", help="Hintergrunddienst mit Taskleistensymbol", parents=[common])
    d.add_argument("--no-tray", action="store_true")
    d.add_argument("--allow-multiple", action="store_true")
    d.set_defaults(func=cmd_daemon)
    t = sub.add_parser("tray", help="Alias fuer 'daemon'", parents=[common])
    t.add_argument("--no-tray", action="store_true")
    t.add_argument("--allow-multiple", action="store_true")
    t.set_defaults(func=cmd_daemon)

    rg = sub.add_parser("restore-gui", help="GUI fuer die Wiederherstellung", parents=[common])
    rg.set_defaults(func=cmd_restore_gui)

    a = sub.add_parser("autostart", help="Autostart ein-/ausschalten", parents=[common])
    a.add_argument("--disable", action="store_true")
    a.set_defaults(func=cmd_autostart)

    pa = sub.add_parser("paths", help="verwendete Pfade zeigen", parents=[common])
    pa.set_defaults(func=cmd_paths)

    ig = sub.add_parser("scan", help="zeigen, was archiviert wuerde", parents=[common])
    ig.add_argument("--list", action="store_true")
    ig.set_defaults(func=cmd_ignored)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
        return 130
    except (crypto.CryptoError, FileNotFoundError, ValueError, LookupError,
            NotADirectoryError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
