"""Adaptive Konsolidierung der Historie.

Ziel: mit moeglichst wenigen Eingriffen viel Platz sparen und dabei
Loeschungen deutlich vorsichtiger behandeln als Aenderungen.

Begriffe
--------
Eine *Version* einer Datei ist ein Lauf aufeinanderfolgender Snapshots mit
demselben Inhalts-Hash. Wird eine Version durch eine neuere ersetzt, ist die
alte Version ein *Konsolidierungskandidat*:

  * Die Datei existiert danach weiter (keine Loeschung). Loeschungen werden
    nie konsolidiert, die letzte Fassung einer geloeschten Datei bleibt also
    dauerhaft erhalten.
  * Die alte Version belegt mindestens 'min_change_size' im Archiv
    (Default 100 MiB). Kleine Aenderungen bleiben unangetastet, weil sie
    kaum Platz kosten.

Konsolidieren heisst: in den alten Snapshots zeigt der Pfad ab jetzt auf die
*neuere* Fassung, der alte Blob wird geloescht. Der Zeitpunkt bleibt also
rekonstruierbar, nur diese eine Zwischenfassung der Datei ist weg.

Ueberschreitet das Archiv 'max_archive_size', werden Kandidaten von alt nach
neu abgearbeitet, bis 'target_ratio * max_archive_size' erreicht ist. Findet
sich nichts oberhalb der Schwelle, wird die Schwelle halbiert (bis
'min_change_size_floor'), sofern 'relax_threshold' aktiv ist.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .archive import Archive, Snapshot
from .config import Config
from .util import fmt_size, utcnow

log = logging.getLogger("cryptdrive.consolidate")


@dataclass
class Candidate:
    path: str
    old_hash: str
    new_hash: str
    new_entry: dict
    run: list[str] = field(default_factory=list)   # Snapshots mit der alten Fassung
    introduced: str = ""                           # Snapshot, in dem sie entstand
    replaced_at: str = ""                          # Snapshot der Ersetzung
    size: int = 0                                  # freiwerdende Bytes
    age_index: int = 0                             # Position von 'introduced'

    def describe(self) -> str:
        return (f"{self.path} [{self.introduced} -> {self.replaced_at}] "
                f"{fmt_size(self.size)}")


def _load_all(archive: Archive) -> list[Snapshot]:
    return [archive.load_snapshot(sid) for sid in archive.snapshot_ids()]


def analyse(archive: Archive, snapshots: list[Snapshot] | None = None
            ) -> tuple[list[Candidate], dict]:
    """Kandidaten und Statistik der Historie bestimmen."""
    snaps = snapshots if snapshots is not None else _load_all(archive)
    stats = {"snapshots": len(snaps), "protected_deletions": 0,
             "candidates": 0, "candidate_bytes": 0, "shared_blobs": 0}
    if len(snaps) < 2:
        return [], stats

    ids = [s.id for s in snaps]
    # Wer referenziert welchen Blob? (Snapshot, Pfad)
    refs: dict[str, set[tuple[str, str]]] = {}
    for snap in snaps:
        for path, entry in snap.files.items():
            refs.setdefault(entry["h"], set()).add((snap.id, path))

    all_paths: set[str] = set()
    for snap in snaps:
        all_paths.update(snap.files)

    candidates: list[Candidate] = []
    for path in sorted(all_paths):
        current: str | None = None
        run_start = 0
        for i, snap in enumerate(snaps):
            entry = snap.files.get(path)
            digest = entry["h"] if entry else None
            if digest == current:
                continue
            if current is not None:
                run = ids[run_start:i]
                if digest is None:
                    # Loeschung: alte Fassung bleibt geschuetzt.
                    stats["protected_deletions"] += 1
                else:
                    owned = {(sid, path) for sid in run}
                    external = refs.get(current, set()) - owned
                    if external:
                        # Blob wird noch woanders gebraucht (Dedup), kein Gewinn.
                        stats["shared_blobs"] += 1
                    else:
                        cand = Candidate(
                            path=path, old_hash=current, new_hash=digest,
                            new_entry=dict(entry), run=run,
                            introduced=ids[run_start], replaced_at=snap.id,
                            size=archive.blob_size(current), age_index=run_start,
                        )
                        if cand.size > 0:
                            candidates.append(cand)
            current = digest
            run_start = i
        # der letzte Lauf ist die aktuelle Fassung und bleibt unberuehrt

    # aelteste Aenderung zuerst, bei Gleichstand die groessere
    candidates.sort(key=lambda c: (c.age_index, -c.size, c.path))
    stats["candidates"] = len(candidates)
    stats["candidate_bytes"] = sum(c.size for c in candidates)
    return candidates, stats


def plan(cfg: Config, archive: Archive) -> dict:
    """Vorschau: was wuerde konsolidiert werden?"""
    total = archive.total_size()
    limit = cfg.max_archive_bytes
    target = int(limit * cfg.history.target_ratio)
    candidates, stats = analyse(archive)
    selected, threshold, freed = _select(cfg, candidates, total, target)
    return {
        "archive_bytes": total,
        "limit": limit,
        "target": target,
        "over_limit": total > limit,
        "threshold": threshold,
        "selected": [c.describe() for c in selected],
        "selected_count": len(selected),
        "would_free": freed,
        **stats,
    }


def _select(cfg: Config, candidates: list[Candidate], total: int, target: int
            ) -> tuple[list[Candidate], int, int]:
    """Kandidaten von alt nach neu auswaehlen, bis das Ziel erreicht ist."""
    if total <= target:
        return [], cfg.min_change_bytes, 0
    threshold = cfg.min_change_bytes
    floor = max(1, cfg.min_change_floor_bytes)
    remaining = list(candidates)
    selected: list[Candidate] = []
    freed = 0
    while True:
        picked_this_round = False
        for cand in list(remaining):
            if total - freed <= target:
                break
            if cand.size >= threshold:
                selected.append(cand)
                remaining.remove(cand)
                freed += cand.size
                picked_this_round = True
        if total - freed <= target:
            break
        if not cfg.history.relax_threshold or threshold <= floor:
            break
        if not picked_this_round and not remaining:
            break
        threshold = max(floor, threshold // 2)
        if threshold == floor and not any(c.size >= floor for c in remaining):
            break
    return selected, threshold, freed


def _apply(archive: Archive, cand: Candidate, snap_by_id: dict[str, Snapshot],
           refs: dict[str, set[tuple[str, str]]]) -> int:
    """Alte Fassung durch die neuere ersetzen und den Blob loeschen."""
    stamp = utcnow().isoformat(timespec="seconds")
    for sid in cand.run:
        snap = snap_by_id[sid]
        entry = dict(cand.new_entry)
        entry["cons"] = True          # Inhalt stammt aus einem spaeteren Stand
        snap.files[cand.path] = entry
        snap.notes.append({
            "at": stamp, "action": "consolidated", "path": cand.path,
            "dropped": cand.old_hash, "now": cand.new_hash, "bytes": cand.size,
        })
        archive.save_snapshot(snap)     # erst Manifeste, dann Blob loeschen
        refs.setdefault(cand.new_hash, set()).add((sid, cand.path))
        refs.get(cand.old_hash, set()).discard((sid, cand.path))
    if refs.get(cand.old_hash):
        return 0
    freed = archive.delete_blob(cand.old_hash)
    refs.pop(cand.old_hash, None)
    return freed


def consolidate(cfg: Config, archive: Archive, progress_cb=None,
                dry_run: bool = False, force: bool = False) -> dict:
    """Historie konsolidieren, falls das Archiv sein Limit ueberschreitet."""
    total = archive.total_size()
    limit = cfg.max_archive_bytes
    target = int(limit * cfg.history.target_ratio)
    if force:
        # Manueller Lauf: alle Kandidaten oberhalb der Schwelle abarbeiten.
        target = 0
    report = {
        "ran": False, "before": total, "after": total, "limit": limit,
        "target": target, "threshold": cfg.min_change_bytes, "changes": [],
        "blobs_removed": 0, "bytes_freed": 0, "still_over": False,
    }
    if total <= limit and not force:
        log.debug("Archiv %s <= Limit %s, keine Konsolidierung",
                  fmt_size(total), fmt_size(limit))
        return report

    if progress_cb:
        progress_cb(f"Archiv {fmt_size(total)} ueber Limit {fmt_size(limit)}, analysiere Historie")

    snaps = _load_all(archive)
    candidates, stats = analyse(archive, snaps)
    report.update({k: stats[k] for k in ("protected_deletions", "candidates", "shared_blobs")})
    selected, threshold, expected = _select(cfg, candidates, total, target)
    report["threshold"] = threshold
    report["selected_count"] = len(selected)

    if dry_run:
        report["changes"] = [c.describe() for c in selected]
        report["after"] = total - expected
        report["still_over"] = (total - expected) > limit
        return report

    if not selected:
        report["still_over"] = total > limit
        log.warning("Archiv %s ueber Limit %s, aber kein Kandidat >= %s gefunden "
                    "(Loeschungen bleiben geschuetzt).",
                    fmt_size(total), fmt_size(limit), fmt_size(threshold))
        return report

    snap_by_id = {s.id: s for s in snaps}
    refs: dict[str, set[tuple[str, str]]] = {}
    for snap in snaps:
        for path, entry in snap.files.items():
            refs.setdefault(entry["h"], set()).add((snap.id, path))

    freed = 0
    for n, cand in enumerate(selected, 1):
        if progress_cb:
            progress_cb(f"Konsolidiere {n}/{len(selected)}: {cand.path} ({fmt_size(cand.size)})")
        got = _apply(archive, cand, snap_by_id, refs)
        if got:
            report["blobs_removed"] += 1
            freed += got
        report["changes"].append(cand.describe())
        log.info("Konsolidiert: %s", cand.describe())

    report["ran"] = True
    report["bytes_freed"] = freed
    report["after"] = archive.total_size()
    report["still_over"] = report["after"] > limit
    if report["still_over"]:
        log.warning("Archiv weiterhin ueber Limit: %s > %s",
                    fmt_size(report["after"]), fmt_size(limit))
    if progress_cb:
        progress_cb(f"Konsolidierung fertig, {fmt_size(freed)} freigegeben")
    return report
