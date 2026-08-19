"""Rekonstruktion eines Stands aus dem Archiv."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .archive import Archive
from .config import Config
from .crypto import CryptoError, Keyring
from .util import fmt_size, parse_snapshot_id

log = logging.getLogger("cryptdrive.restore")


@dataclass
class RestoreProgress:
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    current: str = ""
    message: str = ""

    @property
    def percent(self) -> float:
        if self.bytes_total:
            return 100.0 * self.bytes_done / self.bytes_total
        return 0.0


@dataclass
class RestoreResult:
    snapshot_id: str = ""
    files: int = 0
    bytes_written: int = 0
    skipped: int = 0
    errors: list = field(default_factory=list)


@dataclass
class SnapshotInfo:
    id: str
    created: datetime
    files: int
    source_bytes: int
    stored_bytes: int
    consolidated: int

    def label(self) -> str:
        local = self.created.astimezone().strftime("%Y-%m-%d %H:%M")
        extra = f", {self.consolidated} konsolidiert" if self.consolidated else ""
        return (f"{local}  |  {self.files} Dateien  |  {fmt_size(self.source_bytes)}"
                f"  |  Archivanteil {fmt_size(self.stored_bytes)}{extra}")


def list_snapshots(archive: Archive) -> list[SnapshotInfo]:
    out = []
    for sid in archive.snapshot_ids():
        snap = archive.load_snapshot(sid)
        stats = snap.stats or {}
        out.append(SnapshotInfo(
            id=sid,
            created=snap.created,
            files=int(stats.get("files", len(snap.files))),
            source_bytes=int(stats.get("source_bytes", snap.total_size())),
            stored_bytes=int(stats.get("stored_bytes", 0)),
            consolidated=sum(1 for e in snap.files.values() if e.get("cons")),
        ))
    return out


def resolve_snapshot(archive: Archive, when: datetime | str | None) -> str:
    """Snapshot-ID zu einem Datum/Zeitpunkt bestimmen (neuester <= when)."""
    ids = archive.snapshot_ids()
    if not ids:
        raise LookupError("Das Archiv enthaelt noch keine Snapshots.")
    if when is None:
        return ids[-1]
    if isinstance(when, str):
        if when in ids:
            return when
        if when in ("latest", "neuester"):
            return ids[-1]
        when = _parse_when(when)
    sid = archive.snapshot_at(when)
    if sid is None:
        raise LookupError(
            f"Kein Snapshot vor {when.astimezone():%Y-%m-%d %H:%M} "
            f"(aeltester ist {parse_snapshot_id(ids[0]).astimezone():%Y-%m-%d %H:%M})."
        )
    return sid


def _parse_when(text: str) -> datetime:
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            naive = datetime.strptime(text, fmt)
            if fmt in ("%Y-%m-%d", "%d.%m.%Y"):
                naive = naive.replace(hour=23, minute=59, second=59)
            return naive.astimezone()
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.astimezone()
    except ValueError as exc:
        raise ValueError(f"Datum nicht verstanden: {text!r}") from exc


def restore(cfg: Config, keyring: Keyring, when=None, dest: Path | None = None,
            subpaths: list[str] | None = None, overwrite: bool = False,
            progress_cb=None, verify: bool = True) -> RestoreResult:
    """Einen Stand in einen Zielordner schreiben.

    dest=None bedeutet: zurueck in den Quellordner (nur mit overwrite=True).
    """
    archive = Archive(cfg.archive_path, keyring, cfg.compression)
    sid = resolve_snapshot(archive, when)
    snap = archive.load_snapshot(sid)
    target_root = Path(dest) if dest else cfg.source_path
    result = RestoreResult(snapshot_id=sid)

    selected = {
        rel: entry for rel, entry in snap.files.items()
        if not subpaths or any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in subpaths)
    }
    progress = RestoreProgress(
        files_total=len(selected),
        bytes_total=sum(int(e["s"]) for e in selected.values()),
        message=f"Snapshot {sid} -> {target_root}",
    )

    def report():
        if progress_cb:
            try:
                progress_cb(progress)
            except Exception:
                log.debug("progress callback failed", exc_info=True)

    report()
    target_root.mkdir(parents=True, exist_ok=True)
    for rel in sorted(selected):
        entry = selected[rel]
        progress.current = rel
        out = target_root / rel
        try:
            if out.exists() and not overwrite:
                st = out.stat()
                if st.st_size == int(entry["s"]) and st.st_mtime_ns == int(entry["m"]):
                    result.skipped += 1
                    progress.files_done += 1
                    progress.bytes_done += int(entry["s"])
                    report()
                    continue
            written = archive.get_blob(entry["h"], out, verify=verify)
            os.utime(out, ns=(int(entry["m"]), int(entry["m"])))
            result.files += 1
            result.bytes_written += written
        except (OSError, CryptoError) as exc:
            msg = f"{rel}: {exc}"
            result.errors.append(msg)
            log.warning("Wiederherstellung fehlgeschlagen: %s", msg)
        progress.files_done += 1
        progress.bytes_done += int(entry["s"])
        report()

    progress.current = ""
    progress.message = (f"Fertig: {result.files} Dateien ({fmt_size(result.bytes_written)}), "
                        f"{result.skipped} uebersprungen, {len(result.errors)} Fehler")
    report()
    log.info("Restore %s: %s", sid, progress.message)
    return result


def verify_archive(cfg: Config, keyring: Keyring, snapshot: str | None = None,
                   progress_cb=None) -> dict:
    """Alle Blobs eines Snapshots (oder des gesamten Archivs) entschluesseln und pruefen."""
    archive = Archive(cfg.archive_path, keyring, cfg.compression)
    if snapshot:
        digests = {e["h"] for e in archive.load_snapshot(
            resolve_snapshot(archive, snapshot)).files.values()}
    else:
        digests = archive.referenced_hashes()
    report = {"checked": 0, "bytes": 0, "missing": [], "broken": []}
    for n, digest in enumerate(sorted(digests), 1):
        if progress_cb:
            progress_cb(f"Pruefe {n}/{len(digests)}")
        if not archive.has_blob(digest):
            report["missing"].append(digest)
            continue
        try:
            report["bytes"] += archive.verify_blob(digest)
            report["checked"] += 1
        except (OSError, CryptoError) as exc:
            report["broken"].append(f"{digest}: {exc}")
    return report
