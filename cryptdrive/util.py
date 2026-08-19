"""Kleine Helfer: Groessenangaben, Zeitformate, Single-Instance-Lock."""
from __future__ import annotations

import datetime as _dt
import os
import re
import sys
from pathlib import Path

_UNITS = {
    "b": 1,
    "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
    "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
    "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4,
}


def parse_size(value) -> int:
    """'100 MiB' -> 104857600. Zahlen werden als Bytes interpretiert."""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower().replace("_", "")
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*([a-z]*)", text)
    if not m:
        raise ValueError(f"Ungueltige Groessenangabe: {value!r}")
    num, unit = float(m.group(1)), m.group(2) or "b"
    if unit not in _UNITS:
        raise ValueError(f"Unbekannte Einheit in {value!r}")
    return int(num * _UNITS[unit])


def fmt_size(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024 or unit == "TiB":
            return f"{num:,.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} TiB"


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def snapshot_id(when: _dt.datetime | None = None) -> str:
    when = (when or utcnow()).astimezone(_dt.timezone.utc)
    return when.strftime("%Y%m%dT%H%M%SZ")


def parse_snapshot_id(sid: str) -> _dt.datetime:
    return _dt.datetime.strptime(sid, "%Y%m%dT%H%M%SZ").replace(tzinfo=_dt.timezone.utc)


def to_local(when: _dt.datetime) -> _dt.datetime:
    return when.astimezone()


def fmt_local(when: _dt.datetime | None) -> str:
    if when is None:
        return "noch nie"
    return to_local(when).strftime("%Y-%m-%d %H:%M")


class SingleInstanceLock:
    """Prozessuebergreifendes Lock ueber eine exklusiv geoeffnete Datei."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self, blocking: bool = False) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        except OSError:
            return False
        try:
            if sys.platform == "win32":
                import msvcrt
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(fd, mode, 1)
            else:
                import fcntl
                flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, flags)
        except OSError:
            os.close(fd)
            return False
        self._fh = fd
        os.truncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(self._fh, 0, os.SEEK_SET)
                msvcrt.locking(self._fh, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(self._fh)
        self._fh = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"Lock bereits vergeben: {self.path}")
        return self

    def __exit__(self, *exc):
        self.release()
        return False
