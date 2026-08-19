"""Schluesselableitung, Kompression und authentifizierte Verschluesselung.

Format eines Blobs (eine Datei oder ein Snapshot-Manifest):

    Header (30 Byte, unverschluesselt, aber authentifiziert):
        magic          4  b"CDB1"
        version        1  = 1
        comp           1  0 = keine Kompression, 1 = zstd
        blob_salt     16  Zufall, leitet den Blob-Schluessel ab
        plain_size     8  Groesse des Klartexts (little endian)
    danach Segmente des (ggf. komprimierten) Datenstroms:
        last           1  1 = letztes Segment (authentifiziert ueber AAD)
        len            4  Laenge des Ciphertexts (little endian)
        ct           len  AES-256-GCM (Ciphertext + 16 Byte Tag)

Pro Blob wird ueber HKDF ein eigener Schluessel abgeleitet, dadurch sind
fortlaufende Nonces (Segmentindex) sicher. AAD enthaelt Header, Segmentindex
und ein Endmarkierungs-Flag, damit Kuerzung, Umsortierung und Vertauschen von
Segmenten erkannt werden.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import struct
from dataclasses import dataclass
from pathlib import Path

import zstandard as zstd
from argon2.low_level import Type as Argon2Type, hash_secret_raw
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"CDB1"
FORMAT_VERSION = 1
HEADER_LEN = 4 + 1 + 1 + 16 + 8
SEGMENT_SIZE = 4 * 1024 * 1024
TAG_LEN = 16
KDF_ID = "argon2id"
ARCHIVE_META = "archive.json"


class CryptoError(Exception):
    pass


def _hkdf(key: bytes, info: bytes, salt: bytes = b"", length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(key)


def derive_master_key(password: str, salt: bytes, time_cost: int, memory_kib: int,
                      parallelism: int) -> bytes:
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=time_cost,
        memory_cost=memory_kib,
        parallelism=parallelism,
        hash_len=32,
        type=Argon2Type.ID,
    )


@dataclass
class Keyring:
    """Alle aus dem Master-Key abgeleiteten Teilschluessel."""

    master: bytes

    def __post_init__(self):
        if len(self.master) != 32:
            raise CryptoError("Master-Key muss 32 Byte lang sein.")
        self.enc_key = _hkdf(self.master, b"cryptdrive/v1/blob-enc")
        self.hash_key = _hkdf(self.master, b"cryptdrive/v1/content-hash")
        self.verify_key = _hkdf(self.master, b"cryptdrive/v1/verify")

    @property
    def verifier(self) -> str:
        return hmac.new(self.verify_key, b"cryptdrive-key-check", hashlib.sha256).hexdigest()

    def hasher(self):
        """Keyed BLAKE2b: die Objektnamen im Archiv verraten nichts ueber den Inhalt."""
        return hashlib.blake2b(key=self.hash_key, digest_size=32)

    def hash_bytes(self, data: bytes) -> str:
        h = self.hasher()
        h.update(data)
        return h.hexdigest()

    def hash_file(self, path: Path, chunk: int = 1 << 20) -> tuple[str, int]:
        h = self.hasher()
        size = 0
        with open(path, "rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                size += len(block)
                h.update(block)
        return h.hexdigest(), size


# --------------------------------------------------------------------------
# Archiv-Metadaten (Salt und KDF-Parameter, damit der Key allein aus dem
# Passwort rekonstruierbar ist)
# --------------------------------------------------------------------------

def write_archive_meta(archive_root: Path, salt: bytes, keyring: Keyring, crypto_cfg) -> dict:
    meta = {
        "format": FORMAT_VERSION,
        "app": "cryptdrive",
        "archive_id": secrets.token_hex(8),
        "kdf": {
            "id": KDF_ID,
            "salt": salt.hex(),
            "time_cost": crypto_cfg.argon2_time_cost,
            "memory_kib": crypto_cfg.argon2_memory_kib,
            "parallelism": crypto_cfg.argon2_parallelism,
        },
        "verifier": keyring.verifier,
        "cipher": "AES-256-GCM",
        "compression": "zstd",
    }
    archive_root.mkdir(parents=True, exist_ok=True)
    tmp = archive_root / (ARCHIVE_META + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    os.replace(tmp, archive_root / ARCHIVE_META)
    return meta


def read_archive_meta(archive_root: Path) -> dict:
    path = Path(archive_root) / ARCHIVE_META
    if not path.exists():
        raise CryptoError(f"Kein Archiv unter {archive_root} ({ARCHIVE_META} fehlt).")
    return json.loads(path.read_text(encoding="utf-8"))


def keyring_from_password(archive_root: Path, password: str) -> Keyring:
    """Master-Key aus Passwort + Archiv-Metadaten rekonstruieren."""
    meta = read_archive_meta(archive_root)
    kdf = meta["kdf"]
    if kdf.get("id") != KDF_ID:
        raise CryptoError(f"Unbekannte KDF: {kdf.get('id')}")
    key = derive_master_key(
        password,
        bytes.fromhex(kdf["salt"]),
        int(kdf["time_cost"]),
        int(kdf["memory_kib"]),
        int(kdf["parallelism"]),
    )
    keyring = Keyring(key)
    if not hmac.compare_digest(keyring.verifier, meta["verifier"]):
        raise CryptoError("Falsches Passwort (Verifier passt nicht).")
    return keyring


def save_key_file(path: Path, key: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    if os.name == "nt":
        # Nur der aktuelle Benutzer und SYSTEM duerfen lesen.
        import subprocess
        user = os.environ.get("USERNAME", "")
        try:
            subprocess.run(["icacls", str(path), "/inheritance:r",
                            "/grant:r", f"{user}:F", "/grant:r", "SYSTEM:F"],
                           check=False, capture_output=True)
        except OSError:
            pass


def load_key_file(path: Path) -> Keyring:
    key = Path(path).read_bytes()
    return Keyring(key)


# --------------------------------------------------------------------------
# Blob-Ein-/Ausgabe
# --------------------------------------------------------------------------

def _blob_key(enc_key: bytes, blob_salt: bytes) -> bytes:
    return _hkdf(enc_key, b"cryptdrive/v1/blob", salt=blob_salt)


def _nonce(index: int) -> bytes:
    return index.to_bytes(12, "big")


def _pack_header(comp: int, blob_salt: bytes, plain_size: int) -> bytes:
    return MAGIC + bytes([FORMAT_VERSION, comp]) + blob_salt + struct.pack("<Q", plain_size)


def _unpack_header(header: bytes) -> tuple[int, bytes, int]:
    if len(header) != HEADER_LEN or header[:4] != MAGIC:
        raise CryptoError("Kein cryptdrive-Blob (Magic falsch).")
    version, comp = header[4], header[5]
    if version != FORMAT_VERSION:
        raise CryptoError(f"Unbekannte Blob-Version {version}.")
    blob_salt = header[6:22]
    (plain_size,) = struct.unpack("<Q", header[22:30])
    return comp, blob_salt, plain_size


class _SegmentWriter:
    """Puffert Bytes und schreibt sie segmentweise verschluesselt heraus."""

    def __init__(self, fh, aesgcm: AESGCM, header: bytes):
        self.fh = fh
        self.aesgcm = aesgcm
        self.header = header
        self.buf = bytearray()
        self.index = 0
        self.written = len(header)

    def _emit(self, data: bytes, last: bool) -> None:
        flag = b"\x01" if last else b"\x00"
        aad = self.header + struct.pack("<I", self.index) + flag
        ct = self.aesgcm.encrypt(_nonce(self.index), data, aad)
        self.fh.write(flag + struct.pack("<I", len(ct)))
        self.fh.write(ct)
        self.written += 5 + len(ct)
        self.index += 1

    def feed(self, data: bytes) -> None:
        if not data:
            return
        self.buf += data
        while len(self.buf) >= SEGMENT_SIZE:
            self._emit(bytes(self.buf[:SEGMENT_SIZE]), False)
            del self.buf[:SEGMENT_SIZE]

    def finish(self) -> int:
        self._emit(bytes(self.buf), True)
        self.buf.clear()
        return self.written


def _compressor(cfg, level: int | None = None, threads: int | None = None):
    """zstd-Kompressor gemaess Konfiguration.

    long_window aktiviert Long-Distance-Matching (gut fuer grosse Dateien mit
    weit auseinander liegenden Wiederholungen), threads=0 nutzt alle Kerne.
    """
    lvl = cfg.level if level is None else level
    if threads is None:
        threads = cfg.threads if cfg.threads else -1
    params = zstd.ZstdCompressionParameters.from_level(
        lvl,
        write_checksum=0,          # Integritaet sichert bereits AES-GCM
        enable_ldm=1 if cfg.long_window else 0,
        threads=threads,
    )
    return zstd.ZstdCompressor(compression_params=params)


def encrypt_file_to_blob(src: Path, dst: Path, keyring: Keyring, compress: bool,
                         comp_cfg, read_chunk: int = 1 << 20) -> tuple[int, int, int]:
    """Datei komprimieren (optional), verschluesseln, nach dst schreiben.

    Rueckgabe: (Bytes auf der Platte, Klartextgroesse, comp-Flag)
    """
    blob_salt = secrets.token_bytes(16)
    plain_size = os.path.getsize(src)
    comp_flag = 1 if compress else 0
    header = _pack_header(comp_flag, blob_salt, plain_size)
    aesgcm = AESGCM(_blob_key(keyring.enc_key, blob_salt))

    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(header)
        writer = _SegmentWriter(fout, aesgcm, header)
        if compress:
            cctx = _compressor(comp_cfg)
            cobj = cctx.compressobj()
            while True:
                block = fin.read(read_chunk)
                if not block:
                    break
                writer.feed(cobj.compress(block))
            writer.feed(cobj.flush(zstd.COMPRESSOBJ_FLUSH_FINISH))
        else:
            while True:
                block = fin.read(read_chunk)
                if not block:
                    break
                writer.feed(block)
        written = writer.finish()
        fout.flush()
        os.fsync(fout.fileno())
    return written, plain_size, comp_flag


def encrypt_bytes_to_blob(data: bytes, dst: Path, keyring: Keyring, comp_cfg,
                          compress: bool = True) -> int:
    blob_salt = secrets.token_bytes(16)
    comp_flag = 1 if compress else 0
    header = _pack_header(comp_flag, blob_salt, len(data))
    aesgcm = AESGCM(_blob_key(keyring.enc_key, blob_salt))
    payload = _compressor(comp_cfg, threads=0).compress(data) if compress else data
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as fout:
        fout.write(header)
        writer = _SegmentWriter(fout, aesgcm, header)
        writer.feed(payload)
        written = writer.finish()
        fout.flush()
        os.fsync(fout.fileno())
    return written


def _iter_plain(src_fh, keyring: Keyring):
    """Blob entschluesseln und (dekomprimiert) stueckweise liefern."""
    header = src_fh.read(HEADER_LEN)
    comp, blob_salt, plain_size = _unpack_header(header)
    aesgcm = AESGCM(_blob_key(keyring.enc_key, blob_salt))
    dobj = zstd.ZstdDecompressor().decompressobj() if comp else None
    index = 0
    seen_last = False
    while True:
        record = src_fh.read(5)
        if not record:
            break
        if len(record) != 5:
            raise CryptoError("Blob unvollstaendig (Segmentkopf).")
        flag = record[:1]
        (ct_len,) = struct.unpack("<I", record[1:5])
        ct = src_fh.read(ct_len)
        if len(ct) != ct_len:
            raise CryptoError("Blob unvollstaendig (Segmentdaten).")
        aad = header + struct.pack("<I", index) + flag
        try:
            data = aesgcm.decrypt(_nonce(index), ct, aad)
        except Exception as exc:
            raise CryptoError(f"Authentifizierung fehlgeschlagen (Segment {index}).") from exc
        seen_last = flag == b"\x01"
        index += 1
        if data:
            yield dobj.decompress(data) if dobj else data
        if seen_last:
            break
    if not seen_last:
        raise CryptoError("Blob wurde abgeschnitten (Endsegment fehlt).")
    if dobj is not None:
        rest = dobj.flush()
        if rest:
            yield rest


def decrypt_blob_to_file(src: Path, dst: Path, keyring: Keyring,
                         expected_hash: str | None = None) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    hasher = keyring.hasher() if expected_hash else None
    total = 0
    tmp = dst.with_name(dst.name + ".cdpart")
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        for block in _iter_plain(fin, keyring):
            fout.write(block)
            total += len(block)
            if hasher:
                hasher.update(block)
    if hasher and hasher.hexdigest() != expected_hash:
        tmp.unlink(missing_ok=True)
        raise CryptoError(f"Hash-Pruefung fehlgeschlagen fuer {dst}")
    os.replace(tmp, dst)
    return total


def decrypt_blob_to_bytes(src: Path, keyring: Keyring) -> bytes:
    out = bytearray()
    with open(src, "rb") as fin:
        for block in _iter_plain(fin, keyring):
            out += block
    return bytes(out)


def verify_blob(src: Path, keyring: Keyring, expected_hash: str | None = None) -> int:
    """Blob vollstaendig entschluesseln, ohne zu schreiben. Liefert Klartextgroesse."""
    hasher = keyring.hasher() if expected_hash else None
    total = 0
    with open(src, "rb") as fin:
        for block in _iter_plain(fin, keyring):
            total += len(block)
            if hasher:
                hasher.update(block)
    if hasher and hasher.hexdigest() != expected_hash:
        raise CryptoError(f"Hash-Pruefung fehlgeschlagen fuer {src.name}")
    return total


# --------------------------------------------------------------------------
# Kompressionsentscheidung
# --------------------------------------------------------------------------

def should_compress(path: Path, size: int, cfg) -> bool:
    """Entscheidet, ob Kompression sich lohnt.

    1. Endung auf der Skip-Liste (mp4, jpg, zip, ...) -> nein.
    2. Grosse Dateien: Stichprobe komprimieren; wenn kaum Gewinn -> nein.
    """
    ext = path.suffix.lower().lstrip(".")
    if ext in {e.lower().lstrip(".") for e in cfg.skip_extensions}:
        return False
    if size == 0:
        return False
    probe_min = parse_size_local(cfg.probe_min_file_size)
    if size < probe_min:
        return True
    probe_bytes = parse_size_local(cfg.probe_bytes)
    try:
        with open(path, "rb") as fh:
            sample = fh.read(probe_bytes)
    except OSError:
        return True
    if not sample:
        return True
    packed = zstd.ZstdCompressor(level=3).compress(sample)
    return (len(packed) / len(sample)) < cfg.probe_min_ratio


def parse_size_local(value) -> int:
    from .util import parse_size
    return parse_size(value)
