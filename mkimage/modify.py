"""Modify existing FAT32 images (add/remove files)."""
from __future__ import annotations

import os
import struct
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from mkimage.platform import _is_windows, _resolve, _run, _which

if TYPE_CHECKING:
    from mkimage import Config


def modify_img(cfg: Config, image: str, add_paths: list[str],
               remove_paths: list[str]) -> None:
    """Add or remove files from an existing FAT32 image.

    On Linux/macOS with mtools, edits in place via mcopy/mdel. On Windows
    (or wherever mtools is missing), reads the image in pure Python, applies
    the changes, and rewrites it — no admin, no external tools.
    """
    if _is_windows() or not _which("mcopy"):
        _modify_img_pure_python(cfg, image, add_paths, remove_paths)
        return

    img = _resolve(image)

    # Remove files first
    for path in remove_paths:
        cfg.log(f"  Removing ::{path}")
        _run(cfg, ["mdel", "-i", img, f"::{path}"], check=False,
             verbose=cfg.verbose)

    # Add files
    for path in add_paths:
        p = Path(path)
        if not p.exists():
            cfg.log(f"  Warning: {path} not found, skipping")
            continue
        if p.is_file():
            cfg.log(f"  Adding {p.name}")
            _run(cfg, ["mcopy", "-i", img, "-o", str(p.resolve()), f"::{p.name}"],
                 verbose=cfg.verbose)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    rel = str(PurePosixPath(f.relative_to(p)))
                    # Create directories
                    parts = PurePosixPath(rel).parts
                    for i in range(1, len(parts)):
                        d = str(PurePosixPath(*parts[:i]))
                        _run(cfg, ["mmd", "-i", img, f"::{d}"], check=False)
                    cfg.log(f"  Adding {rel}")
                    _run(cfg, ["mcopy", "-i", img, "-o", str(f.resolve()),
                               f"::{rel}"], verbose=cfg.verbose)

    cfg.log(f"  [OK] Modified {image}")


# ---------------------------------------------------------------------------
# Pure-Python FAT32 read / modify / rewrite (no mtools, no root)
# ---------------------------------------------------------------------------

def _modify_img_pure_python(cfg: Config, image: str, add_paths: list[str],
                            remove_paths: list[str]) -> None:
    """Read a FAT32 image, apply add/remove, and rewrite it.

    Supports bare-FAT and MBR-wrapped single-FAT32-partition images. GPT and
    multi-partition images are not supported by this path.
    """
    from mkimage.builders.img import _write_fat32_image

    meta = _read_fat32_image(image)
    files = meta["files"]  # dict: posix relpath -> bytes

    # --- apply removals (case-insensitive, '/'-normalized) ---
    rm_norm = {r.replace("\\", "/").lstrip("/").lower() for r in remove_paths}
    if rm_norm:
        kept = {k: v for k, v in files.items() if k.lower() not in rm_norm}
        for k in files:
            if k.lower() in rm_norm:
                cfg.log(f"  Removing {k}")
        files = kept

    # --- apply additions ---
    for path in add_paths:
        p = Path(path)
        if not p.exists():
            cfg.log(f"  Warning: {path} not found, skipping")
            continue
        if p.is_file():
            cfg.log(f"  Adding {p.name}")
            files[p.name] = p.read_bytes()
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    rel = str(PurePosixPath(f.relative_to(p)))
                    cfg.log(f"  Adding {rel}")
                    files[rel] = f.read_bytes()

    # --- rewrite: stage current contents to temp files, rebuild the image ---
    with tempfile.TemporaryDirectory() as staging:
        staged: dict[str, str] = {}
        for rel, data in files.items():
            dest = Path(staging) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            staged[rel] = str(dest)

        size_mb = max(1, os.path.getsize(image) // (1024 * 1024))
        _write_fat32_image(cfg, staged, image, size_mb,
                           str(meta["label"]), 0, mbr=bool(meta["mbr"]))

    cfg.log(f"  [OK] Modified {image} ({len(files)} files)")


def _read_fat32_image(image: str) -> dict[str, object]:
    """Parse a FAT32 image. Returns {files, label, mbr}.

    files: {posix-relative-path: bytes}. Handles 8.3 + VFAT long names and
    nested directories. Detects bare-FAT vs MBR-wrapped layout.
    """
    with open(image, "rb") as f:
        data = f.read()

    def _is_fat32_bpb(off: int) -> bool:
        return data[off + 0x52:off + 0x5A] == b"FAT32   "

    # --- find where the FAT32 volume starts ---
    part_lba, mbr = 0, False
    if _is_fat32_bpb(0):
        part_lba, mbr = 0, False
    elif data[510:512] == b"\x55\xAA":
        if data[512:520] == b"EFI PART":
            raise RuntimeError(
                "modify: GPT images are not supported by the pure-Python "
                "modify path. Rebuild the image instead.")
        ptype = data[0x1BE + 4]
        lba = struct.unpack("<I", data[0x1BE + 8:0x1BE + 12])[0]
        if ptype in (0x0B, 0x0C) and lba > 0 and _is_fat32_bpb(lba * 512):
            part_lba, mbr = lba, True
        else:
            raise RuntimeError("modify: no FAT32 partition found in image.")
    else:
        raise RuntimeError("modify: not a recognized FAT32/MBR image.")

    bps = struct.unpack("<H", data[part_lba * 512 + 0x0B:part_lba * 512 + 0x0D])[0] or 512
    base = part_lba * bps
    spc = data[base + 0x0D]
    reserved = struct.unpack("<H", data[base + 0x0E:base + 0x10])[0]
    nfat = data[base + 0x10]
    fatsz = struct.unpack("<I", data[base + 0x24:base + 0x28])[0]
    root_cluster = struct.unpack("<I", data[base + 0x2C:base + 0x30])[0]
    label = data[base + 71:base + 82].decode("ascii", "replace").strip() or "UEFITOOLS"

    fat_start = base + reserved * bps
    data_start = base + (reserved + nfat * fatsz) * bps
    cluster_bytes = spc * bps

    def _cluster_off(c: int) -> int:
        return data_start + (c - 2) * cluster_bytes

    def _chain(c: int) -> list[int]:
        out: list[int] = []
        seen: set[int] = set()
        while 2 <= c < 0x0FFFFFF8 and c not in seen:
            seen.add(c)
            out.append(c)
            c = struct.unpack("<I", data[fat_start + c * 4:fat_start + c * 4 + 4])[0] & 0x0FFFFFFF
        return out

    def _read_chain_bytes(c: int) -> bytes:
        return b"".join(data[_cluster_off(x):_cluster_off(x) + cluster_bytes]
                        for x in _chain(c))

    files: dict[str, bytes] = {}

    def _walk(start_cluster: int, prefix: str) -> None:
        raw = _read_chain_bytes(start_cluster)
        lfn: list[tuple[int, bytes]] = []
        for i in range(0, len(raw), 32):
            ent = raw[i:i + 32]
            if len(ent) < 32 or ent[0] == 0x00:
                break
            if ent[0] == 0xE5:
                lfn = []
                continue
            attr = ent[0x0B]
            if attr == 0x0F:  # long-filename component
                lfn.append((ent[0], ent[1:11] + ent[14:26] + ent[28:32]))
                continue
            if attr & 0x08:   # volume label
                lfn = []
                continue
            if lfn:
                lfn.sort(key=lambda x: x[0] & 0x1F)
                name = b"".join(p[1] for p in lfn).decode("utf-16-le", "replace")
                name = name.split("\x00")[0]
            else:
                b83 = ent[0:8].decode("ascii", "replace").rstrip()
                ext = ent[8:11].decode("ascii", "replace").rstrip()
                name = f"{b83}.{ext}" if ext else b83
            lfn = []
            if name in (".", ".."):
                continue
            first = (struct.unpack("<H", ent[0x14:0x16])[0] << 16) | \
                    struct.unpack("<H", ent[0x1A:0x1C])[0]
            size = struct.unpack("<I", ent[0x1C:0x20])[0]
            rel = f"{prefix}/{name}" if prefix else name
            if attr & 0x10:  # directory
                if first >= 2:
                    _walk(first, rel)
            else:
                files[rel] = _read_chain_bytes(first)[:size] if first >= 2 else b""

    _walk(root_cluster, "")
    return {"files": files, "label": label, "mbr": mbr}
