"""Das Archiv: inhaltsadressierter Blob-Store plus verschluesselte Snapshots.

Layout im (lokal gemounteten) Zielordner, z. B. G:/Meine Ablage/cryptdrive:

    archive.json                 KDF-Salt, Parameter, Key-Verifier (Klartext)
    objects/ab/cd/<hash>.blob    komprimiert + verschluesselter Dateiinhalt
    snapshots/<id>.snap          verschluesseltes Manifest eines Laufs
    tmp/                         Zwischendateien beim Schreiben

Ein Blob wird nur einmal hochgeladen: der Name ist der (verschluesselte, per
BLAKE2b-keyed) Hash des Klartexts. Unveraenderte Dateien kosten also nichts,
identische Dateien werden dedupliziert.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import crypto
from .util import parse_snapshot_id, snapshot_id

OBJECTS = "objects"
SNAPSHOTS = "snapshots"
TMP = "tmp"
SNAP_SUFFIX = ".snap"
BLOB_SUFFIX = ".blob"


@dataclass
class Snapshot:
    """Ein Stand des Quellordners zu einem Zeitpunkt."""

    id: str
    created: datetime
    source: str
    files: dict[str, dict] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    @property
    def when(self) -> datetime:
        return self.created

    def total_size(self) -> int:
        return sum(int(e.get("s", 0)) for e in self.files.values())

    def to_json(self) -> bytes:
        payload = {
            "id": self.id,
            "created": self.created.isoformat(),
            "source": self.source,
            "files": self.files,
            "stats": self.stats,
            "notes": self.notes,
        }
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> "Snapshot":
        raw = json.loads(data.decode("utf-8"))
        return cls(
            id=raw["id"],
            created=datetime.fromisoformat(raw["created"]),
            source=raw.get("source", ""),
            files=raw.get("files", {}),
            stats=raw.get("stats", {}),
            notes=raw.get("notes", []),
        )


class Archive:
    def __init__(self, root: Path, keyring: crypto.Keyring, comp_cfg):
        self.root = Path(root)
        self.keyring = keyring
        self.comp_cfg = comp_cfg

    # ---------------- Struktur ----------------
    def ensure_layout(self) -> None:
        for sub in (OBJECTS, SNAPSHOTS, TMP):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def blob_path(self, digest: str) -> Path:
        return self.root / OBJECTS / digest[:2] / digest[2:4] / (digest + BLOB_SUFFIX)

    def has_blob(self, digest: str) -> bool:
        return self.blob_path(digest).exists()

    def blob_size(self, digest: str) -> int:
        try:
            return self.blob_path(digest).stat().st_size
        except OSError:
            return 0

    def _tmp(self, name: str) -> Path:
        d = self.root / TMP
        d.mkdir(parents=True, exist_ok=True)
        return d / name

    # ---------------- Blobs ----------------
    def put_file(self, src: Path, digest: str, compress: bool) -> int:
        """Datei als Blob ablegen. Liefert die Groesse im Archiv."""
        target = self.blob_path(digest)
        if target.exists():
            return target.stat().st_size
        tmp = self._tmp(digest + ".part")
        try:
            written, _plain, _comp = crypto.encrypt_file_to_blob(
                src, tmp, self.keyring, compress, self.comp_cfg
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        return written

    def get_blob(self, digest: str, dst: Path, verify: bool = True) -> int:
        return crypto.decrypt_blob_to_file(
            self.blob_path(digest), dst, self.keyring,
            expected_hash=digest if verify else None,
        )

    def verify_blob(self, digest: str) -> int:
        return crypto.verify_blob(self.blob_path(digest), self.keyring, expected_hash=digest)

    def delete_blob(self, digest: str) -> int:
        path = self.blob_path(digest)
        try:
            size = path.stat().st_size
        except OSError:
            return 0
        try:
            path.unlink()
        except OSError:
            return 0
        # leere Verzeichnisse aufraeumen
        for parent in (path.parent, path.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                break
        return size

    def iter_blobs(self):
        base = self.root / OBJECTS
        if not base.exists():
            return
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                if name.endswith(BLOB_SUFFIX):
                    full = Path(dirpath) / name
                    try:
                        yield name[: -len(BLOB_SUFFIX)], full.stat().st_size
                    except OSError:
                        continue

    # ---------------- Snapshots ----------------
    def snapshot_ids(self) -> list[str]:
        base = self.root / SNAPSHOTS
        if not base.exists():
            return []
        ids = [p.name[: -len(SNAP_SUFFIX)] for p in base.iterdir()
               if p.name.endswith(SNAP_SUFFIX)]
        return sorted(ids)

    def snapshot_path(self, sid: str) -> Path:
        return self.root / SNAPSHOTS / (sid + SNAP_SUFFIX)

    def load_snapshot(self, sid: str) -> Snapshot:
        data = crypto.decrypt_blob_to_bytes(self.snapshot_path(sid), self.keyring)
        return Snapshot.from_json(data)

    def save_snapshot(self, snap: Snapshot) -> int:
        path = self.snapshot_path(snap.id)
        tmp = self._tmp(snap.id + ".snap.part")
        written = crypto.encrypt_bytes_to_blob(snap.to_json(), tmp, self.keyring, self.comp_cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, path)
        return written

    def latest_snapshot(self) -> Snapshot | None:
        ids = self.snapshot_ids()
        return self.load_snapshot(ids[-1]) if ids else None

    def new_snapshot_id(self, when: datetime | None = None) -> str:
        sid = snapshot_id(when)
        existing = set(self.snapshot_ids())
        if sid not in existing:
            return sid
        # sehr unwahrscheinlich (zwei Laeufe in derselben Sekunde)
        base = parse_snapshot_id(sid)
        for offset in range(1, 60):
            cand = snapshot_id(base.replace(second=(base.second + offset) % 60))
            if cand not in existing:
                return cand
        raise RuntimeError("Keine freie Snapshot-ID gefunden.")

    def snapshot_at(self, when: datetime) -> str | None:
        """Neuester Snapshot, der nicht nach 'when' liegt."""
        best = None
        for sid in self.snapshot_ids():
            if parse_snapshot_id(sid) <= when:
                best = sid
            else:
                break
        return best

    # ---------------- Groessen / GC ----------------
    def total_size(self) -> int:
        total = 0
        for sub in (OBJECTS, SNAPSHOTS):
            base = self.root / sub
            if not base.exists():
                continue
            for dirpath, _dirs, files in os.walk(base):
                for name in files:
                    try:
                        total += (Path(dirpath) / name).stat().st_size
                    except OSError:
                        pass
        return total

    def referenced_hashes(self) -> set[str]:
        refs: set[str] = set()
        for sid in self.snapshot_ids():
            refs.update(e["h"] for e in self.load_snapshot(sid).files.values())
        return refs

    def gc(self, referenced: set[str] | None = None) -> tuple[int, int]:
        """Nicht mehr referenzierte Blobs loeschen. Liefert (Anzahl, Bytes)."""
        refs = referenced if referenced is not None else self.referenced_hashes()
        count = freed = 0
        for digest, size in list(self.iter_blobs()):
            if digest not in refs:
                freed += self.delete_blob(digest) or size
                count += 1
        return count, freed

    def clean_tmp(self) -> None:
        tmp = self.root / TMP
        if tmp.exists():
            for child in tmp.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink()
                except OSError:
                    pass

    def free_space(self) -> int | None:
        try:
            return shutil.disk_usage(self.root).free
        except OSError:
            return None
