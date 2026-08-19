"""Taskleistensymbol: Status anzeigen, Sync anstossen, Restore-GUI oeffnen."""
from __future__ import annotations

import logging
import math
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from .config import Config
from .daemon import Daemon
from .util import fmt_local, fmt_size

log = logging.getLogger("cryptdrive.tray")

COLORS = {
    "idle": (54, 128, 199),
    "syncing": (232, 145, 30),
    "error": (198, 60, 60),
}


def _icon_image(state: str, percent: float = 0.0) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = COLORS.get(state, COLORS["idle"])
    d.ellipse((4, 4, size - 4, size - 4), fill=color)
    if state == "syncing" and percent > 0:
        # Fortschrittsring
        d.arc((4, 4, size - 4, size - 4), start=-90, end=-90 + 3.6 * percent,
              fill=(255, 255, 255), width=6)
    # stilisiertes Schloss
    d.rectangle((22, 30, 42, 48), fill=(255, 255, 255))
    d.arc((24, 16, 40, 36), start=180, end=360, fill=(255, 255, 255), width=5)
    d.rectangle((30, 36, 34, 42), fill=color)
    return img


def _run_detached(args: list[str]) -> None:
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
            getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        subprocess.Popen(args, close_fds=True, **kwargs)
    except OSError as exc:
        log.error("Konnte %s nicht starten: %s", args, exc)


def _python_gui_exe() -> str:
    """pythonw bevorzugen, damit kein Konsolenfenster auftaucht."""
    exe = Path(sys.executable)
    if os.name == "nt" and exe.name.lower() == "python.exe":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


class Tray:
    def __init__(self, daemon: Daemon):
        self.daemon = daemon
        self.cfg: Config = daemon.cfg
        self.icon = pystray.Icon(
            "cryptdrive",
            _icon_image("idle"),
            "cryptdrive",
            menu=self._menu(),
        )
        daemon.listeners.append(self._on_update)

    # ---------------- Menue ----------------
    def _menu(self) -> pystray.Menu:
        d = self.daemon

        def status_text(_item=None):
            labels = {"idle": "Bereit", "syncing": "Synchronisiert", "error": "Achtung"}
            text = labels.get(d.state, d.state)
            if d.state == "syncing" and d.progress:
                p = d.progress
                text += f": {p.phase} {p.percent:.0f} %"
                if p.current:
                    name = p.current if len(p.current) < 40 else "..." + p.current[-37:]
                    text += f" ({name})"
            elif d.last_error:
                text += f": {d.last_error[:60]}"
            return text

        return pystray.Menu(
            pystray.MenuItem(lambda i: "cryptdrive", None, enabled=False),
            pystray.MenuItem(status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda i: f"Ordner:  {fmt_size(d.stats['source_bytes'])}"
                          f"  ({d.stats['source_files']} Dateien)",
                self._open_source),
            pystray.MenuItem(
                lambda i: f"Archiv:  {fmt_size(d.stats['archive_bytes'])}"
                          f"  ({d.stats['snapshot_count']} Snapshots)",
                self._open_archive),
            pystray.MenuItem(lambda i: f"Letzter Sync:  {fmt_local(d.last_sync)}", None,
                             enabled=False),
            pystray.MenuItem(lambda i: f"Naechster Lauf:  {fmt_local(d.next_run())}", None,
                             enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Jetzt synchronisieren", self._sync_now,
                             enabled=lambda i: d.state != "syncing", default=True),
            pystray.MenuItem("Wiederherstellen ...", self._restore_gui),
            pystray.MenuItem("Groessen aktualisieren", self._refresh),
            pystray.MenuItem("Log anzeigen", self._open_log),
            pystray.MenuItem("Konfiguration oeffnen", self._open_config),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Beenden", self._quit),
        )

    # ---------------- Aktionen ----------------
    def _sync_now(self, *_):
        self.daemon.request_sync()

    def _refresh(self, *_):
        threading.Thread(target=self.daemon.refresh_sizes, daemon=True).start()

    def _restore_gui(self, *_):
        _run_detached([_python_gui_exe(), "-m", "cryptdrive", "restore-gui",
                       "--config", str(self.cfg.path)])

    def _open_log(self, *_):
        self._open_path(self.cfg.log_file)

    def _open_config(self, *_):
        self._open_path(self.cfg.path)

    def _open_source(self, *_):
        self._open_path(self.cfg.source_path)

    def _open_archive(self, *_):
        self._open_path(self.cfg.archive_path)

    @staticmethod
    def _open_path(path: Path):
        try:
            if os.name == "nt":
                os.startfile(str(path))  # noqa: S606
            else:
                webbrowser.open(f"file://{path}")
        except OSError as exc:
            log.warning("Konnte %s nicht oeffnen: %s", path, exc)

    def _quit(self, *_):
        log.info("Tray beendet, Daemon wird gestoppt")
        self.daemon.stop()
        self.icon.stop()

    # ---------------- Aktualisierung ----------------
    def _on_update(self, daemon: Daemon):
        percent = daemon.progress.percent if daemon.progress else 0.0
        try:
            self.icon.icon = _icon_image(daemon.state, percent)
            self.icon.title = "cryptdrive\n" + "\n".join(daemon.text_lines())[:120]
            self.icon.update_menu()
        except Exception:
            log.debug("Tray-Update fehlgeschlagen", exc_info=True)

    def run(self) -> None:
        self._on_update(self.daemon)
        self.icon.run()


def run_tray(daemon: Daemon) -> None:
    Tray(daemon).run()
