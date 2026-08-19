"""Der eigentliche Sync: Scan, inkrementeller Upload, Snapshot, Konsolidierung."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import crypto, scanner
from .archive import Archive, Snapshot
from .config import Config
from .consolidate import consolidate
from .crypto import Keyring, should_compress
from .state import LocalIndex
from .util import fmt_size, utcnow

log = logging.getLogger("cryptdrive.sync")


@dataclass
class Progress:
    phase: str = "idle"          # scan | upload | snapshot | consolidate | gc | done
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0         # zu uebertragende Klartextbytes
    bytes_done: int = 0
    current: str = ""
    message: str = ""

    @property
    def percent(self) -> float:
        if self.bytes_total:
            return 100.0 * self.bytes_done / self.bytes_total
        if self.files_total:
            return 100.0 * self.files_done / self.files_total
        return 0.0


@dataclass
class SyncResult:
    snapshot_id: str = ""
    files: int = 0
    source_bytes: int = 0
    added: int = 0
    modified: int = 0
    deleted: int = 0
    uploaded_blobs: int = 0
    uploaded_plain: int = 0
    uploaded_stored: int = 0
    archive_bytes: int = 0
    consolidated: dict = field(default_factory=dict)
    gc_removed: int = 0
    gc_freed: int = 0
    errors: list = field(default_factory=list)
    duration_s: float = 0.0

    def summary(self) -> str:
        return (
            f"Snapshot {self.snapshot_id}: {self.files} Dateien "
            f"({fmt_size(self.source_bytes)}), neu {self.added}, geaendert {self.modified}, "
            f"geloescht {self.deleted}, hochgeladen {fmt_size(self.uploaded_stored)} "
            f"in {self.uploaded_blobs} Objekten, Archiv {fmt_size(self.archive_bytes)}"
        )


def open_archive(cfg: Config, keyring: Keyring) -> Archive:
    archive = Archive(cfg.archive_path, keyring, cfg.compression)
    archive.ensure_layout()
    return archive


def run_sync(cfg: Config, keyring: Keyring, progress_cb=None,
             consolidation: bool = True) -> SyncResult:
    started = utcnow()
    result = SyncResult()
    progress = Progress(phase="scan", message="Quellordner wird gelesen")

    def report():
        if progress_cb:
            try:
                progress_cb(progress)
            except Exception:  # das Tray darf den Sync nie kippen
                log.debug("progress callback failed", exc_info=True)

    report()
    archive = open_archive(cfg, keyring)
    archive.clean_tmp()

    def on_error(path: Path, exc: Exception):
        msg = f"{path}: {exc}"
        result.errors.append(msg)
        log.warning("Uebersprungen: %s", msg)

    # ---------------- 1. Scan ----------------
    entries = list(scanner.scan(cfg, on_error=on_error))
    progress.files_total = len(entries)
    progress.bytes_total = sum(e.size for e in entries)
    result.files = len(entries)
    result.source_bytes = progress.bytes_total
    progress.phase = "upload"
    progress.message = f"{len(entries)} Dateien, {fmt_size(progress.bytes_total)}"
    report()
    log.info("Scan: %d Dateien, %s", len(entries), fmt_size(progress.bytes_total))

    previous = archive.latest_snapshot()
    prev_files = previous.files if previous else {}

    index = LocalIndex(cfg.index_db)
    files: dict[str, dict] = {}
    try:
        for entry in entries:
            progress.current = entry.rel
            digest = index.lookup(entry.rel, entry.size, entry.mtime_ns)
            if digest is None:
                try:
                    digest, real_size = keyring.hash_file(entry.path)
                except OSError as exc:
                    on_error(entry.path, exc)
                    progress.files_done += 1
                    progress.bytes_done += entry.size
                    report()
                    continue
                entry.size = real_size
                index.remember(entry.rel, entry.size, entry.mtime_ns, digest)

            prev = prev_files.get(entry.rel)
            csize = int(prev["c"]) if prev and prev.get("h") == digest and prev.get("c") else 0

            if not archive.has_blob(digest):
                compress = should_compress(entry.path, entry.size, cfg.compression)
                try:
                    csize = archive.put_file(entry.path, digest, compress)
                except (OSError, crypto.CryptoError) as exc:
                    on_error(entry.path, exc)
                    progress.files_done += 1
                    progress.bytes_done += entry.size
                    report()
                    continue
                result.uploaded_blobs += 1
                result.uploaded_plain += entry.size
                result.uploaded_stored += csize
            elif not csize:
                csize = archive.blob_size(digest)

            files[entry.rel] = {"h": digest, "s": entry.size,
                                "m": entry.mtime_ns, "c": csize}
            if prev is None:
                result.added += 1
            elif prev.get("h") != digest:
                result.modified += 1

            progress.files_done += 1
            progress.bytes_done += entry.size
            report()

        result.deleted = sum(1 for rel in prev_files if rel not in files)
        index.prune(set(files))
        index.commit()

        # ---------------- 2. Snapshot ----------------
        progress.phase = "snapshot"
        progress.current = ""
        progress.message = "Snapshot wird geschrieben"
        report()

        sid = archive.new_snapshot_id(started)
        snap = Snapshot(
            id=sid,
            created=started,
            source=str(cfg.source_path),
            files=files,
            stats={
                "files": result.files,
                "source_bytes": result.source_bytes,
                "stored_bytes": sum(int(e["c"]) for e in files.values()),
                "added": result.added,
                "modified": result.modified,
                "deleted": result.deleted,
                "errors": len(result.errors),
            },
        )
        archive.save_snapshot(snap)
        result.snapshot_id = sid
        log.info("Snapshot %s geschrieben (%d Dateien)", sid, len(files))

        # ---------------- 3. Konsolidierung ----------------
        if consolidation:
            progress.phase = "consolidate"
            progress.message = "Historie wird geprueft"
            report()
            report_data = consolidate(cfg, archive, progress_cb=lambda m: (
                setattr(progress, "message", m), report()))
            result.consolidated = report_data
            result.gc_removed = report_data.get("blobs_removed", 0)
            result.gc_freed = report_data.get("bytes_freed", 0)

        result.archive_bytes = archive.total_size()
        index.last_sync = started
        index.set("last_snapshot", result.snapshot_id)
        index.set("source_bytes", result.source_bytes)
        index.set("source_files", result.files)
        index.set("archive_bytes", result.archive_bytes)
        index.set("snapshot_count", len(archive.snapshot_ids()))
    finally:
        index.close()

    archive.clean_tmp()
    result.duration_s = (utcnow() - started).total_seconds()
    progress.phase = "done"
    progress.message = result.summary()
    report()
    log.info(result.summary())
    return result


def source_stats(cfg: Config) -> tuple[int, int]:
    return scanner.scan_summary(cfg)


def last_sync_time(cfg: Config) -> datetime | None:
    try:
        with LocalIndex(cfg.index_db) as index:
            return index.last_sync
    except Exception:
        return None


def utc(dt: datetime | None) -> datetime | None:
    return dt.astimezone(timezone.utc) if dt else None
