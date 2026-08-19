"""Konfiguration (TOML) und Standard-Pfade."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .util import parse_size

APP_NAME = "cryptdrive"

# Dateitypen, die praktisch nie weiter komprimierbar sind. Fuer diese wird die
# Kompression uebersprungen (spart bei grossen Dateien sehr viel Rechenzeit).
DEFAULT_SKIP_EXTENSIONS = [
    # Archive / Container
    "zip", "7z", "rar", "gz", "tgz", "bz2", "tbz2", "xz", "txz", "zst", "tzst",
    "lz4", "lzma", "cab", "arj", "lha", "jar", "war", "whl", "apk", "aar",
    "crx", "xpi", "nupkg", "vsix", "epub", "docx", "xlsx", "pptx", "odt", "ods",
    "odp", "odg", "iso", "dmg", "appx", "msix", "pak", "asar", "rpm", "deb",
    # Bilder
    "jpg", "jpeg", "jpe", "jfif", "png", "gif", "webp", "heic", "heif", "avif",
    "jxl", "j2k", "jp2", "jxr",
    # Video
    "mp4", "m4v", "mkv", "webm", "avi", "mov", "wmv", "flv", "f4v", "mpg",
    "mpeg", "m2ts", "mts", "vob", "3gp", "ogv", "rmvb", "braw", "r3d",
    # Audio
    "mp3", "aac", "m4a", "m4b", "ogg", "oga", "opus", "wma", "flac", "ape",
    "alac", "mpc", "spx",
    # Dokumente / Fonts / sonstiges bereits Komprimiertes
    "pdf", "woff", "woff2", "swf", "chm",
    # Verschluesseltes (Entropie am Maximum)
    "gpg", "pgp", "age", "enc", "crypt", "kdbx",
    # VM-/Container-Images
    "qcow2", "vmdk", "vdi", "ova",
    # Machine-Learning-Gewichte (Float-Rauschen, kaum kompressibel)
    "safetensors", "gguf", "ggml", "onnx", "npz",
]

DEFAULT_IGNORE_PATTERNS = '''\
# Zentrale ignore-Datei von cryptdrive (git-wildmatch Syntax wie .gitignore).
# Diese Muster gelten zusaetzlich zu allen .gitignore-Dateien im Quellordner,
# relativ zum Quellordner.

# --- Betriebssystem ---
Thumbs.db
ehthumbs.db
desktop.ini
Desktop.ini
$RECYCLE.BIN/
System Volume Information/
.DS_Store
._*
.Spotlight-V100
.Trashes

# --- Temporaere Dateien und Backups ---
*.tmp
*.temp
*.swp
*.swo
*~
*.bak
*.old
*.orig
*.rej
*.partial
*.crdownload
*.part
*.download
~$*
.~lock.*

# --- Caches / Build-Artefakte (auch ausserhalb von Git-Repos) ---
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.mypy_cache/
.ruff_cache/
.tox/
.venv/
venv/
node_modules/
.next/
.nuxt/
.parcel-cache/
.turbo/
.gradle/
.cache/
dist/
build/
target/
obj/
.vs/
.idea/
CMakeFiles/
*.o
*.obj
*.class
*.pdb
*.ilk
*.log

# --- Cloud-/Sync-Metadaten ---
.dropbox
.dropbox.attr
.dropbox.cache/
.syncthing.*
*.icloud

# --- Grosse Wegwerf-Dateien ---
pagefile.sys
hiberfil.sys
swapfile.sys
crashdump*.dmp
'''


def state_dir() -> Path:
    base = os.environ.get("CRYPTDRIVE_HOME")
    if base:
        return Path(base)
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / APP_NAME
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / APP_NAME


def default_config_path() -> Path:
    return state_dir() / "config.toml"


@dataclass
class ScheduleCfg:
    daily_time: str = "03:00"         # lokale Uhrzeit des taeglichen Laufs
    catch_up: bool = True             # verpassten Lauf sofort nachholen
    catch_up_grace_minutes: int = 2   # Wartezeit nach Programmstart


@dataclass
class CompressionCfg:
    level: int = 19                   # zstd Level (1..22)
    threads: int = 0                  # 0 = alle Kerne
    long_window: bool = True
    skip_extensions: list[str] = field(default_factory=lambda: list(DEFAULT_SKIP_EXTENSIONS))
    probe_bytes: str | int = "256 KiB"    # Stichprobe zur Kompressibilitaetspruefung
    probe_min_file_size: str | int = "4 MiB"
    probe_min_ratio: float = 0.95         # schlechter -> unkomprimiert speichern


@dataclass
class CryptoCfg:
    # Argon2id: bewusst stark, aber effizient genug fuer taegliche Laeufe.
    argon2_time_cost: int = 4
    argon2_memory_kib: int = 262144   # 256 MiB
    argon2_parallelism: int = 4


@dataclass
class HistoryCfg:
    max_archive_size: str | int = "100 GiB"    # ab hier wird konsolidiert
    target_ratio: float = 0.90                 # Konsolidierung laeuft bis 90 % davon
    min_change_size: str | int = "100 MiB"     # nur Aenderungen ab dieser Groesse
    relax_threshold: bool = True               # Schwelle halbieren, wenn nichts passt
    min_change_size_floor: str | int = "8 MiB"
    protect_deletions: bool = True             # Loeschungen nie konsolidieren


@dataclass
class IgnoreCfg:
    use_gitignore: bool = True
    use_git_info_exclude: bool = True
    include_git_dirs: bool = True     # .git-Ordner mitsichern (Repo-Historie)
    central_file: str = ""            # leer -> state_dir()/ignore.conf
    extra_patterns: list[str] = field(default_factory=list)
    follow_symlinks: bool = False
    max_file_size: str | int = 0      # 0 = unbegrenzt


@dataclass
class Config:
    source: str = ""
    archive: str = ""
    schedule: ScheduleCfg = field(default_factory=ScheduleCfg)
    compression: CompressionCfg = field(default_factory=CompressionCfg)
    crypto: CryptoCfg = field(default_factory=CryptoCfg)
    history: HistoryCfg = field(default_factory=HistoryCfg)
    ignore: IgnoreCfg = field(default_factory=IgnoreCfg)
    path: Path = field(default_factory=default_config_path)

    # --- abgeleitete Werte ---
    @property
    def source_path(self) -> Path:
        return Path(self.source).expanduser()

    @property
    def archive_path(self) -> Path:
        return Path(self.archive).expanduser()

    @property
    def key_file(self) -> Path:
        return state_dir() / "master.key"

    @property
    def index_db(self) -> Path:
        return state_dir() / "index.sqlite3"

    @property
    def status_file(self) -> Path:
        return state_dir() / "status.json"

    @property
    def log_file(self) -> Path:
        return state_dir() / "cryptdrive.log"

    @property
    def lock_file(self) -> Path:
        return state_dir() / "sync.lock"

    @property
    def ignore_file(self) -> Path:
        return Path(self.ignore.central_file) if self.ignore.central_file else state_dir() / "ignore.conf"

    @property
    def max_archive_bytes(self) -> int:
        return parse_size(self.history.max_archive_size)

    @property
    def min_change_bytes(self) -> int:
        return parse_size(self.history.min_change_size)

    @property
    def min_change_floor_bytes(self) -> int:
        return parse_size(self.history.min_change_size_floor)

    def skip_ext(self) -> set[str]:
        return {e.lower().lstrip(".") for e in self.compression.skip_extensions}


def _fill(dc_cls, data: dict):
    known = set(dc_cls.__dataclass_fields__)
    return dc_cls(**{k: v for k, v in data.items() if k in known})


def load_config(path: Path | str | None = None) -> Config:
    cfg_path = Path(path) if path else default_config_path()
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Keine Konfiguration unter {cfg_path}. Zuerst 'cryptdrive init' ausfuehren."
        )
    raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    cfg = Config(
        source=raw.get("source", ""),
        archive=raw.get("archive", ""),
        schedule=_fill(ScheduleCfg, raw.get("schedule", {})),
        compression=_fill(CompressionCfg, raw.get("compression", {})),
        crypto=_fill(CryptoCfg, raw.get("crypto", {})),
        history=_fill(HistoryCfg, raw.get("history", {})),
        ignore=_fill(IgnoreCfg, raw.get("ignore", {})),
        path=cfg_path,
    )
    if not cfg.source or not cfg.archive:
        raise ValueError(f"'source' und 'archive' muessen in {cfg_path} gesetzt sein.")
    return cfg


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        if len(value) > 6:
            return "[\n    " + ",\n    ".join(_toml_value(v) for v in value) + ",\n]"
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_config(cfg: Config, path: Path | None = None) -> Path:
    target = Path(path or cfg.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# cryptdrive Konfiguration",
        "",
        f"source = {_toml_value(cfg.source)}",
        f"archive = {_toml_value(cfg.archive)}",
    ]
    for section, obj in (
        ("schedule", cfg.schedule),
        ("compression", cfg.compression),
        ("crypto", cfg.crypto),
        ("history", cfg.history),
        ("ignore", cfg.ignore),
    ):
        lines += ["", f"[{section}]"]
        for key, value in asdict(obj).items():
            lines.append(f"{key} = {_toml_value(value)}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def ensure_ignore_file(cfg: Config) -> Path:
    """Zentrale ignore-Datei anlegen, falls sie fehlt."""
    path = cfg.ignore_file
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_IGNORE_PATTERNS, encoding="utf-8")
    return path
