"""Hintergrunddienst: taeglicher Lauf um 03:00 mit Nachholen verpasster Laeufe."""
from __future__ import annotations

import logging
import logging.handlers
import threading
from dataclasses import asdict
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from .archive import Archive
from .config import Config
from .crypto import Keyring
from .state import LocalIndex, write_status
from .sync import Progress, run_sync
from .util import SingleInstanceLock, fmt_local, fmt_size, utcnow

log = logging.getLogger("cryptdrive")


def setup_logging(cfg: Config, level=logging.INFO, to_console: bool = False) -> None:
    root = logging.getLogger("cryptdrive")
    root.setLevel(level)
    root.handlers.clear()
    cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        cfg.log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(handler)
    if to_console:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        root.addHandler(console)


def parse_daily_time(text: str) -> dtime:
    hour, _, minute = text.partition(":")
    return dtime(hour=int(hour), minute=int(minute or 0))


def most_recent_due(now: datetime, daily: dtime) -> datetime:
    """Letzter faelliger Termin <= now (lokale Zeit)."""
    today = now.replace(hour=daily.hour, minute=daily.minute, second=0, microsecond=0)
    return today if today <= now else today - timedelta(days=1)


def next_due(now: datetime, daily: dtime) -> datetime:
    return most_recent_due(now, daily) + timedelta(days=1)


class Daemon:
    """Haelt den Zustand, plant Laeufe und stellt sie dem Tray zur Verfuegung."""

    def __init__(self, cfg: Config, keyring: Keyring):
        self.cfg = cfg
        self.keyring = keyring
        self.daily = parse_daily_time(cfg.schedule.daily_time)
        self._stop = threading.Event()
        self._trigger = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_push = 0.0
        self.state = "idle"                # idle | syncing | error
        self.progress: Progress | None = None
        self.last_error = ""
        self.last_result = ""
        self.listeners: list = []          # Callbacks fuer das Tray
        self.stats = self._load_stats()

    # ---------------- Zustand ----------------
    def _load_stats(self) -> dict:
        stats = {"last_sync": None, "source_bytes": 0, "source_files": 0,
                 "archive_bytes": 0, "snapshot_count": 0, "last_snapshot": ""}
        try:
            with LocalIndex(self.cfg.index_db) as index:
                last = index.last_sync
                stats["last_sync"] = last.isoformat() if last else None
                for key in ("source_bytes", "source_files", "archive_bytes", "snapshot_count"):
                    stats[key] = int(index.get(key, 0) or 0)
                stats["last_snapshot"] = index.get("last_snapshot", "") or ""
        except Exception:
            log.debug("Konnte lokalen Index nicht lesen", exc_info=True)
        return stats

    @property
    def last_sync(self) -> datetime | None:
        raw = self.stats.get("last_sync")
        return datetime.fromisoformat(raw) if raw else None

    def next_run(self) -> datetime:
        return next_due(datetime.now().astimezone(), self.daily)

    def status(self) -> dict:
        prog = asdict(self.progress) if self.progress else None
        data = {
            "state": self.state,
            "progress": prog,
            "percent": round(self.progress.percent, 1) if self.progress else 0.0,
            "last_error": self.last_error,
            "last_result": self.last_result,
            "next_run": self.next_run().isoformat(),
            "config": str(self.cfg.path),
            "source": str(self.cfg.source_path),
            "archive": str(self.cfg.archive_path),
            **self.stats,
        }
        return data

    def _push(self, force: bool = False) -> None:
        write_status(self.cfg.status_file, self.status())
        for cb in list(self.listeners):
            try:
                cb(self)
            except Exception:
                log.debug("Listener-Fehler", exc_info=True)

    # ---------------- Sync ----------------
    def sync_now(self, consolidation: bool = True) -> None:
        """Blockierender Lauf. Laeuft bereits einer, kehrt der Aufruf zurueck."""
        if not self._lock.acquire(blocking=False):
            log.info("Sync laeuft bereits, Anfrage ignoriert")
            return
        file_lock = SingleInstanceLock(self.cfg.lock_file)
        try:
            if not file_lock.acquire():
                self.last_error = "Ein anderer cryptdrive-Prozess synchronisiert gerade."
                log.warning(self.last_error)
                return
            self.state = "syncing"
            self.last_error = ""
            self._push(force=True)
            import time as _time

            def on_progress(progress: Progress):
                self.progress = progress
                now = _time.monotonic()
                if progress.phase in ("scan", "snapshot", "done") or now - self._last_push > 0.5:
                    self._last_push = now
                    self._push()

            result = run_sync(self.cfg, self.keyring, progress_cb=on_progress,
                              consolidation=consolidation)
            self.last_result = result.summary()
            self.stats = self._load_stats()
            self.state = "error" if result.errors else "idle"
            if result.errors:
                self.last_error = f"{len(result.errors)} Datei(en) uebersprungen: {result.errors[0]}"
        except Exception as exc:
            self.state = "error"
            self.last_error = str(exc)
            log.exception("Sync fehlgeschlagen")
        finally:
            file_lock.release()
            self.progress = None
            self._push(force=True)
            self._lock.release()

    def request_sync(self) -> None:
        """Aus dem Tray heraus einen Lauf anstossen (nicht blockierend)."""
        threading.Thread(target=self.sync_now, name="cryptdrive-manual", daemon=True).start()

    # ---------------- Zeitplan ----------------
    def _due_now(self) -> bool:
        now = datetime.now().astimezone()
        due = most_recent_due(now, self.daily)
        last = self.last_sync
        if last is None:
            return True
        return last.astimezone() < due

    def _loop(self) -> None:
        grace = max(0, int(self.cfg.schedule.catch_up_grace_minutes)) * 60
        if self.cfg.schedule.catch_up and self._due_now():
            log.info("Verpasster Lauf wird nachgeholt (Startverzoegerung %d s)", grace)
            if not self._stop.wait(grace):
                self.sync_now()
        while not self._stop.is_set():
            now = datetime.now().astimezone()
            wait = max(30.0, min(300.0, (next_due(now, self.daily) - now).total_seconds()))
            if self._trigger.wait(timeout=wait):
                self._trigger.clear()
                if self._stop.is_set():
                    break
                self.sync_now()
                continue
            if self._stop.is_set():
                break
            if self._due_now():
                log.info("Taeglicher Lauf um %s", self.cfg.schedule.daily_time)
                self.sync_now()

    def start(self) -> None:
        self._push(force=True)
        self._thread = threading.Thread(target=self._loop, name="cryptdrive-scheduler",
                                        daemon=True)
        self._thread.start()
        log.info("Daemon gestartet, naechster Lauf %s", fmt_local(self.next_run()))

    def stop(self) -> None:
        self._stop.set()
        self._trigger.set()
        if self._thread:
            self._thread.join(timeout=5)

    def trigger(self) -> None:
        self._trigger.set()

    # ---------------- Anzeige ----------------
    def refresh_sizes(self) -> None:
        """Groessen neu bestimmen, ohne zu synchronisieren."""
        try:
            archive = Archive(self.cfg.archive_path, self.keyring, self.cfg.compression)
            self.stats["archive_bytes"] = archive.total_size()
            self.stats["snapshot_count"] = len(archive.snapshot_ids())
            with LocalIndex(self.cfg.index_db) as index:
                index.set("archive_bytes", self.stats["archive_bytes"])
                index.set("snapshot_count", self.stats["snapshot_count"])
        except Exception as exc:
            log.warning("Groessen konnten nicht bestimmt werden: %s", exc)
        self._push(force=True)

    def text_lines(self) -> list[str]:
        s = self.stats
        lines = [
            f"Ordner (unkomprimiert): {fmt_size(s['source_bytes'])} in {s['source_files']} Dateien",
            f"Archiv (mit Historie):  {fmt_size(s['archive_bytes'])} "
            f"in {s['snapshot_count']} Snapshots",
            f"Letzte Synchronisierung: {fmt_local(self.last_sync)}",
            f"Naechster Lauf: {fmt_local(self.next_run())}",
        ]
        if self.state == "syncing" and self.progress:
            p = self.progress
            lines.insert(0, f"Laeuft: {p.phase} {p.percent:.0f} % ({p.files_done}/{p.files_total})")
        if self.last_error:
            lines.append(f"Hinweis: {self.last_error}")
        return lines
