"""Kleine GUI fuer die Rekonstruktion: Zeitpunkt waehlen und wiederherstellen.

Funktioniert auch auf einem frischen Rechner ohne Konfiguration: dann einfach
Archivordner angeben und das Passwort eintippen, der Schluessel wird daraus
neu abgeleitet.
"""
from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import crypto
from .archive import Archive
from .config import Config, load_config
from .restore import list_snapshots, restore
from .util import fmt_size


class RestoreApp:
    def __init__(self, cfg: Config | None, config_error: str = ""):
        self.cfg = cfg or Config()
        self.config_error = config_error
        self.keyring: crypto.Keyring | None = None
        self.snapshots: list = []
        self.events: queue.Queue = queue.Queue()
        self.busy = False

        self.root = tk.Tk()
        self.root.title("cryptdrive - Wiederherstellung")
        self.root.geometry("900x640")
        self.root.minsize(760, 560)
        self._build()
        self.root.after(120, self._drain)
        if self.cfg.archive:
            self.root.after(200, self.load_snapshots)

    # ---------------- Aufbau ----------------
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        top = ttk.LabelFrame(self.root, text="Archiv und Schluessel")
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Archivordner:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.archive_var = tk.StringVar(value=self.cfg.archive)
        ttk.Entry(top, textvariable=self.archive_var, width=70).grid(
            row=0, column=1, sticky="we", padx=6)
        ttk.Button(top, text="Durchsuchen ...", command=self._pick_archive).grid(
            row=0, column=2, padx=6)

        self.key_mode = tk.StringVar(value="local" if self.cfg.key_file.exists() else "password")
        ttk.Radiobutton(top, text="Lokalen Schluessel verwenden", value="local",
                        variable=self.key_mode, command=self._toggle_key).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=6)
        ttk.Radiobutton(top, text="Schluessel aus Passwort ableiten", value="password",
                        variable=self.key_mode, command=self._toggle_key).grid(
            row=2, column=0, sticky="w", padx=6)
        self.password_var = tk.StringVar()
        self.password_entry = ttk.Entry(top, textvariable=self.password_var, show="*", width=40)
        self.password_entry.grid(row=2, column=1, sticky="w", padx=6)
        ttk.Button(top, text="Snapshots laden", command=self.load_snapshots).grid(
            row=2, column=2, padx=6, pady=4)
        top.columnconfigure(1, weight=1)
        self._toggle_key()

        mid = ttk.LabelFrame(self.root, text="Zeitpunkt waehlen")
        mid.pack(fill="both", expand=True, **pad)

        bar = ttk.Frame(mid)
        bar.pack(fill="x")
        ttk.Label(bar, text="Datum/Zeit:").pack(side="left", padx=6, pady=6)
        self.when_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.when_var, width=22).pack(side="left")
        ttk.Label(bar, text="(JJJJ-MM-TT HH:MM)").pack(side="left", padx=4)
        ttk.Button(bar, text="Passenden Stand markieren",
                   command=self._select_by_date).pack(side="left", padx=8)

        columns = ("datum", "dateien", "groesse", "archiv", "kons")
        self.tree = ttk.Treeview(mid, columns=columns, show="headings", height=12)
        for key, text, width in (
            ("datum", "Stand vom", 170), ("dateien", "Dateien", 90),
            ("groesse", "Inhalt", 110), ("archiv", "Archivanteil", 120),
            ("kons", "konsolidiert", 100),
        ):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=6, pady=6)
        scroll = ttk.Scrollbar(self.tree, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)

        target = ttk.LabelFrame(self.root, text="Ziel")
        target.pack(fill="x", **pad)
        ttk.Label(target, text="Zielordner:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.dest_var = tk.StringVar(value=str(Path.home() / "cryptdrive-restore"))
        ttk.Entry(target, textvariable=self.dest_var, width=70).grid(
            row=0, column=1, sticky="we", padx=6)
        ttk.Button(target, text="Durchsuchen ...", command=self._pick_dest).grid(
            row=0, column=2, padx=6)
        ttk.Label(target, text="Nur diese Unterpfade (optional, mit Komma getrennt):").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=6)
        self.subpaths_var = tk.StringVar()
        ttk.Entry(target, textvariable=self.subpaths_var, width=70).grid(
            row=2, column=1, sticky="we", padx=6, pady=2)
        self.overwrite_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(target, text="Vorhandene Dateien ueberschreiben",
                        variable=self.overwrite_var).grid(row=3, column=1, sticky="w", padx=6)
        target.columnconfigure(1, weight=1)

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", **pad)
        self.start_btn = ttk.Button(bottom, text="Wiederherstellung starten",
                                    command=self._start)
        self.start_btn.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=8)
        self.status_var = tk.StringVar(value=self.config_error or "Bereit")
        ttk.Label(self.root, textvariable=self.status_var).pack(fill="x", padx=14)
        self.log = tk.Text(self.root, height=7, wrap="none")
        self.log.pack(fill="both", padx=14, pady=(2, 10))

    def _toggle_key(self):
        state = "normal" if self.key_mode.get() == "password" else "disabled"
        self.password_entry.configure(state=state)

    # ---------------- Helfer ----------------
    def _pick_archive(self):
        chosen = filedialog.askdirectory(title="Archivordner waehlen")
        if chosen:
            self.archive_var.set(chosen)

    def _pick_dest(self):
        chosen = filedialog.askdirectory(title="Zielordner waehlen")
        if chosen:
            self.dest_var.set(chosen)

    def _say(self, text: str):
        self.status_var.set(text)
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def _current_config(self) -> Config:
        cfg = self.cfg
        cfg.archive = self.archive_var.get().strip()
        return cfg

    def _get_keyring(self) -> crypto.Keyring:
        cfg = self._current_config()
        if not cfg.archive:
            raise ValueError("Bitte einen Archivordner angeben.")
        if self.key_mode.get() == "local":
            if not cfg.key_file.exists():
                raise FileNotFoundError(
                    f"Kein lokaler Schluessel unter {cfg.key_file}. Bitte Passwort verwenden.")
            return crypto.load_key_file(cfg.key_file)
        password = self.password_var.get()
        if not password:
            raise ValueError("Bitte das Passwort eingeben.")
        return crypto.keyring_from_password(cfg.archive_path, password)

    # ---------------- Snapshots ----------------
    def load_snapshots(self):
        try:
            self.keyring = self._get_keyring()
        except Exception as exc:
            messagebox.showerror("Schluessel", str(exc))
            return
        cfg = self._current_config()
        try:
            archive = Archive(cfg.archive_path, self.keyring, cfg.compression)
            self.snapshots = list_snapshots(archive)
        except Exception as exc:
            messagebox.showerror("Archiv", f"Snapshots konnten nicht gelesen werden:\n{exc}")
            return
        self.tree.delete(*self.tree.get_children())
        for info in reversed(self.snapshots):   # neueste oben
            self.tree.insert("", "end", iid=info.id, values=(
                info.created.astimezone().strftime("%Y-%m-%d %H:%M"),
                info.files,
                fmt_size(info.source_bytes),
                fmt_size(info.stored_bytes),
                info.consolidated or "-",
            ))
        if self.snapshots:
            newest = self.snapshots[-1].id
            self.tree.selection_set(newest)
            self.tree.focus(newest)
        self._say(f"{len(self.snapshots)} Snapshots gefunden.")

    def _select_by_date(self):
        from .restore import _parse_when
        if not self.snapshots:
            messagebox.showinfo("Snapshots", "Bitte zuerst die Snapshots laden.")
            return
        try:
            when = _parse_when(self.when_var.get())
        except ValueError as exc:
            messagebox.showerror("Datum", str(exc))
            return
        match = None
        for info in self.snapshots:
            if info.created <= when:
                match = info
        if match is None:
            messagebox.showwarning("Kein Stand", "Vor diesem Zeitpunkt gibt es keinen Snapshot.")
            return
        self.tree.selection_set(match.id)
        self.tree.focus(match.id)
        self.tree.see(match.id)
        self._say(f"Gewaehlt: Stand vom {match.created.astimezone():%Y-%m-%d %H:%M}")

    # ---------------- Restore ----------------
    def _start(self):
        if self.busy:
            return
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Auswahl", "Bitte einen Stand in der Liste waehlen.")
            return
        sid = selection[0]
        dest = Path(self.dest_var.get().strip())
        if not dest:
            messagebox.showinfo("Ziel", "Bitte einen Zielordner angeben.")
            return
        cfg = self._current_config()
        overwrite = self.overwrite_var.get()
        same_as_source = False
        if cfg.source:
            try:
                same_as_source = dest.resolve() == cfg.source_path.resolve()
            except OSError:
                same_as_source = False
        if same_as_source:
            if not messagebox.askyesno(
                    "Achtung",
                    "Das Ziel ist der Quellordner. Bestehende Dateien koennen "
                    "ueberschrieben werden. Fortfahren?"):
                return
        subpaths = [p.strip().replace("\\", "/") for p in self.subpaths_var.get().split(",")
                    if p.strip()]

        self.busy = True
        self.start_btn.configure(state="disabled")
        self.progress.configure(value=0)
        self._say(f"Starte Wiederherstellung von {sid} nach {dest}")

        def worker():
            try:
                result = restore(cfg, self.keyring, when=sid, dest=dest,
                                 subpaths=subpaths or None, overwrite=overwrite,
                                 progress_cb=lambda p: self.events.put(("progress", p)))
                self.events.put(("done", result))
            except Exception as exc:
                self.events.put(("error", f"{exc}\n{traceback.format_exc(limit=2)}"))

        threading.Thread(target=worker, daemon=True).start()

    def _drain(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self.progress.configure(value=payload.percent)
                    self.status_var.set(
                        f"{payload.percent:.0f} %  {payload.files_done}/{payload.files_total}  "
                        f"{payload.current}")
                elif kind == "done":
                    self.busy = False
                    self.start_btn.configure(state="normal")
                    self.progress.configure(value=100)
                    self._say(f"Fertig: {payload.files} Dateien, "
                              f"{fmt_size(payload.bytes_written)} geschrieben, "
                              f"{payload.skipped} uebersprungen, {len(payload.errors)} Fehler")
                    for err in payload.errors[:20]:
                        self._say("  Fehler: " + err)
                elif kind == "error":
                    self.busy = False
                    self.start_btn.configure(state="normal")
                    self._say("Abgebrochen: " + str(payload))
                    messagebox.showerror("Fehler", str(payload))
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def run(self):
        self.root.mainloop()


def main(config_path: str | None = None) -> int:
    cfg = None
    error = ""
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        error = f"Ohne Konfiguration gestartet ({exc}). Archivordner und Passwort angeben."
    RestoreApp(cfg, error).run()
    return 0
