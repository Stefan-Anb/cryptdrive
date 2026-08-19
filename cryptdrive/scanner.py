"""Quellordner durchlaufen und dabei .gitignore-Dateien respektieren.

Regelwerk (wie bei git):
  * die zentrale ignore-Datei gilt fuer den gesamten Quellordner,
  * jede .gitignore gilt fuer ihr Verzeichnis und alle Unterverzeichnisse,
  * tiefer liegende Regeln gewinnen, Negationen (!muster) werden beachtet,
  * ein ausgeschlossenes Verzeichnis wird komplett uebersprungen.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pathspec

from .config import Config, ensure_ignore_file
from .util import parse_size

GITIGNORE = ".gitignore"
GIT_DIR = ".git"


@dataclass(slots=True)
class FileInfo:
    rel: str          # Pfad relativ zum Quellordner, immer mit "/"
    path: Path        # absoluter Pfad
    size: int
    mtime_ns: int


def _spec(lines) -> pathspec.PathSpec | None:
    cleaned = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    if not cleaned:
        return None
    return pathspec.PathSpec.from_lines("gitwildmatch", cleaned)


def _read_spec(path: Path) -> pathspec.PathSpec | None:
    try:
        return _spec(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return None


def _check(spec: pathspec.PathSpec, rel: str) -> bool | None:
    """True = ignorieren, False = explizit wieder einschliessen, None = kein Treffer."""
    check_file = getattr(spec, "check_file", None)
    if check_file is not None:
        return check_file(rel).include
    return True if spec.match_file(rel) else None


class IgnoreStack:
    """Sammelt die geltenden Specs entlang des Verzeichnispfads."""

    def __init__(self, layers: list[tuple[str, pathspec.PathSpec]] | None = None):
        # layer = (Basis-Pfad relativ zum Quellordner, Spec)
        self.layers = list(layers or [])

    def child(self, base_rel: str, specs: list[pathspec.PathSpec]) -> "IgnoreStack":
        if not specs:
            return self
        return IgnoreStack(self.layers + [(base_rel, s) for s in specs])

    def ignored(self, rel: str, is_dir: bool) -> bool:
        candidate = rel + "/" if is_dir else rel
        decision = False
        for base, spec in self.layers:
            if base:
                prefix = base + "/"
                if not candidate.startswith(prefix):
                    continue
                local = candidate[len(prefix):]
            else:
                local = candidate
            result = _check(spec, local)
            if result is not None:
                decision = result  # tiefere Ebene gewinnt
        return decision


def _dir_specs(cfg: Config, directory: Path) -> list[pathspec.PathSpec]:
    specs = []
    if cfg.ignore.use_gitignore:
        gi = directory / GITIGNORE
        if gi.is_file():
            s = _read_spec(gi)
            if s:
                specs.append(s)
    if cfg.ignore.use_git_info_exclude:
        exclude = directory / GIT_DIR / "info" / "exclude"
        if exclude.is_file():
            s = _read_spec(exclude)
            if s:
                specs.append(s)
    return specs


def root_stack(cfg: Config) -> IgnoreStack:
    """Zentrale ignore-Datei plus extra_patterns aus der Konfiguration."""
    ensure_ignore_file(cfg)
    layers: list[tuple[str, pathspec.PathSpec]] = []
    central = _read_spec(cfg.ignore_file)
    if central:
        layers.append(("", central))
    extra = _spec(cfg.ignore.extra_patterns)
    if extra:
        layers.append(("", extra))
    return IgnoreStack(layers)


def _excluded_roots(cfg: Config) -> list[Path]:
    """Pfade, die niemals archiviert werden (Archiv selbst, eigener Status)."""
    from .config import state_dir
    out = []
    for candidate in (cfg.archive_path, state_dir()):
        try:
            out.append(candidate.resolve())
        except OSError:
            out.append(candidate)
    return out


def scan(cfg: Config, on_error=None):
    """Generator ueber alle zu archivierenden Dateien."""
    source = cfg.source_path.resolve()
    if not source.is_dir():
        raise NotADirectoryError(f"Quellordner existiert nicht: {source}")
    forbidden = _excluded_roots(cfg)
    max_size = parse_size(cfg.ignore.max_file_size or 0)
    follow = bool(cfg.ignore.follow_symlinks)

    stack: list[tuple[Path, str, IgnoreStack]] = [(source, "", root_stack(cfg))]
    while stack:
        directory, rel_dir, ignores = stack.pop()
        ignores = ignores.child(rel_dir, _dir_specs(cfg, directory))
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as exc:
            if on_error:
                on_error(directory, exc)
            continue
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=follow)
                is_file = entry.is_file(follow_symlinks=follow)
                is_link = entry.is_symlink()
            except OSError as exc:
                if on_error:
                    on_error(Path(entry.path), exc)
                continue
            if is_link and not follow:
                continue  # Symlinks/Junctions werden nicht archiviert
            if is_dir:
                full = Path(entry.path)
                try:
                    resolved = full.resolve()
                except OSError:
                    resolved = full
                if any(resolved == bad or bad in resolved.parents for bad in forbidden):
                    continue
                if entry.name == GIT_DIR:
                    # Inhalte von .git unterliegen keinen ignore-Regeln.
                    if cfg.ignore.include_git_dirs:
                        stack.append((full, rel, IgnoreStack()))
                    continue
                if ignores.ignored(rel, True):
                    continue
                stack.append((full, rel, ignores))
                continue
            if not is_file:
                continue
            if ignores.ignored(rel, False):
                continue
            try:
                st = entry.stat(follow_symlinks=follow)
            except OSError as exc:
                if on_error:
                    on_error(Path(entry.path), exc)
                continue
            if max_size and st.st_size > max_size:
                continue
            yield FileInfo(rel=rel, path=Path(entry.path), size=st.st_size,
                           mtime_ns=st.st_mtime_ns)


def scan_summary(cfg: Config) -> tuple[int, int]:
    """(Anzahl Dateien, unkomprimierte Gesamtgroesse) des Quellordners."""
    count = total = 0
    for info in scan(cfg):
        count += 1
        total += info.size
    return count, total
