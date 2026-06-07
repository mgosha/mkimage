"""GPT-partitioned image builder (N partitions from cfg.partitions)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from mkimage.files import (
    _calculate_content_size,
    _interpret_size,
    _stage_files,
    collect_files,
)
from mkimage.partition import (
    _check_root,
    _format_partition,
    _populate_partition,
    _setup_loop_device,
    _teardown_loop_device,
    _wait_for_partition,
)
from mkimage.platform import _resolve, _run
from mkimage.tools import ensure_tools
from mkimage.verify import _verify_write

if TYPE_CHECKING:
    from mkimage import Config, PartitionSpec


def _build_gpt_pure_python(cfg: Config, part_info: list[dict[str, object]],
                           output: str) -> None:
    """Write a GPT-partitioned image in pure Python (no root/tools/WSL).

    Lays out a protective MBR, primary + backup GPT headers and partition
    entry arrays (CRC32-checked), and one FAT32 filesystem per partition.
    FAT32 only — non-FAT32 partitions fall back to FAT32 with a warning.
    """
    import struct
    import uuid
    import zlib
    from mkimage.builders.img import _write_fat32_image

    SS = 512
    ALIGN = 2048  # 1 MiB partition alignment
    NENT, ENT = 128, 128  # 128 entries * 128 bytes = 16 KiB = 32 sectors
    ARRAY_SECTORS = (NENT * ENT) // SS  # 32
    ESP_TYPE = uuid.UUID("C12A7328-F81F-11D2-BA4B-00A0C93EC93B").bytes_le
    DATA_TYPE = uuid.UUID("EBD0A0A2-B9E5-4433-87C0-68B6B72699C7").bytes_le

    # --- lay out partitions, generating each FAT32 filesystem to a temp file ---
    layout: list[tuple[int, int, str, bytes, str]] = []  # start,sectors,tmp,type,label
    tmpfiles: list[str] = []
    cur = ALIGN
    try:
        for info in part_info:
            spec = info["spec"]
            fs = str(info["fs_type"])
            label = str(info["label"])
            files = info["files"] or {}
            size_mb = int(info["size_mb"]) or (int(info["content_mb"]) + 16)  # type: ignore[arg-type]
            if fs != "fat32":
                cfg.log(f"  Warning: '{fs}' not supported by the pure-Python "
                        f"GPT writer; using FAT32 for '{label}'.")
            sectors = (size_mb * 1024 * 1024 + SS - 1) // SS
            sectors = ((sectors + ALIGN - 1) // ALIGN) * ALIGN  # align up
            fd, tmp = tempfile.mkstemp(suffix=".part")
            os.close(fd)
            tmpfiles.append(tmp)
            cs = getattr(spec, "cluster_size", 0)
            _write_fat32_image(cfg, files, tmp, sectors * SS // (1024 * 1024),  # type: ignore[arg-type]
                               label[:11], cs, mbr=False)
            is_esp = getattr(spec, "fs_type", "") == "esp"
            layout.append((cur, sectors, tmp, ESP_TYPE if is_esp else DATA_TYPE,
                           label))
            cur += sectors

        total_sectors = cur + ARRAY_SECTORS + 1  # + backup array + backup header
        first_usable, last_usable = 34, total_sectors - 34
        disk_guid = uuid.uuid4().bytes_le

        # --- partition entry array + its CRC ---
        array = bytearray(NENT * ENT)
        for idx, (start, sectors, _tmp, tguid, label) in enumerate(layout):
            off = idx * ENT
            array[off:off + 16] = tguid
            array[off + 16:off + 32] = uuid.uuid4().bytes_le
            struct.pack_into("<Q", array, off + 32, start)
            struct.pack_into("<Q", array, off + 40, start + sectors - 1)
            struct.pack_into("<Q", array, off + 48, 0)  # attributes
            array[off + 56:off + 128] = label.encode("utf-16-le")[:72].ljust(72, b"\x00")
        array_crc = zlib.crc32(bytes(array)) & 0xFFFFFFFF

        def _header(current_lba: int, backup_lba: int, array_lba: int) -> bytes:
            h = bytearray(92)
            h[0:8] = b"EFI PART"
            struct.pack_into("<I", h, 8, 0x00010000)   # revision 1.0
            struct.pack_into("<I", h, 12, 92)          # header size
            struct.pack_into("<Q", h, 24, current_lba)
            struct.pack_into("<Q", h, 32, backup_lba)
            struct.pack_into("<Q", h, 40, first_usable)
            struct.pack_into("<Q", h, 48, last_usable)
            h[56:72] = disk_guid
            struct.pack_into("<Q", h, 72, array_lba)
            struct.pack_into("<I", h, 80, NENT)
            struct.pack_into("<I", h, 84, ENT)
            struct.pack_into("<I", h, 88, array_crc)
            struct.pack_into("<I", h, 16, zlib.crc32(bytes(h)) & 0xFFFFFFFF)
            return bytes(h)

        primary_hdr = _header(1, total_sectors - 1, 2)
        backup_hdr = _header(total_sectors - 1, 1, total_sectors - 1 - ARRAY_SECTORS)

        # --- protective MBR (one 0xEE partition spanning the disk) ---
        pmbr = bytearray(SS)
        pmbr[446 + 4] = 0xEE
        pmbr[446 + 1:446 + 4] = bytes([0x00, 0x02, 0x00])  # CHS first
        pmbr[446 + 5:446 + 8] = bytes([0xFF, 0xFF, 0xFF])  # CHS last
        struct.pack_into("<I", pmbr, 446 + 8, 1)           # first LBA
        struct.pack_into("<I", pmbr, 446 + 12, min(total_sectors - 1, 0xFFFFFFFF))
        pmbr[510], pmbr[511] = 0x55, 0xAA

        # --- assemble the image ---
        with open(output, "wb") as f:
            f.write(pmbr)                              # LBA 0
            f.write(primary_hdr.ljust(SS, b"\x00"))    # LBA 1
            f.write(bytes(array))                      # LBA 2..33
            for start, sectors, tmp, _t, _l in layout:
                f.seek(start * SS)
                with open(tmp, "rb") as pf:
                    f.write(pf.read())
            f.seek((total_sectors - 1 - ARRAY_SECTORS) * SS)
            f.write(bytes(array))                      # backup array
            f.seek((total_sectors - 1) * SS)
            f.write(backup_hdr.ljust(SS, b"\x00"))     # backup header (last LBA)
            f.truncate(total_sectors * SS)
    finally:
        for t in tmpfiles:
            try:
                os.unlink(t)
            except OSError:
                pass


def build_gpt_img(cfg: Config, source_files: dict[str, str],
                   output: str) -> None:
    """Create a GPT disk image with N partitions from cfg.partitions."""
    from mkimage import PartitionSpec
    from mkimage.platform import _is_windows

    out = _resolve(output)

    partitions = cfg.partitions if cfg.partitions else [PartitionSpec("esp")]

    # Calculate sizes for each partition
    part_info: list[dict[str, object]] = []
    for i, part in enumerate(partitions):
        if i == 0:
            files = source_files
        elif part.source_dir:
            files = collect_files(cfg, part.source_dir, [])
        else:
            files = {}
        content_mb = _calculate_content_size(files) if files else 1
        is_esp = part.fs_type == "esp"
        size_mb = _interpret_size(part.size, content_mb, is_esp=is_esp)
        fs_type = "fat32" if part.fs_type == "esp" else part.fs_type
        sgdisk_type = "EF00" if part.fs_type == "esp" else "0700"
        part_info.append({
            "spec": part,
            "files": files,
            "content_mb": content_mb,
            "size_mb": size_mb,
            "fs_type": fs_type,
            "sgdisk_type": sgdisk_type,
            "label": part.label[:11],
        })

    # Calculate total image size
    sized_total = sum(
        int(p["size_mb"]) for p in part_info if int(p["size_mb"]) > 0  # type: ignore[arg-type]
    )
    total_mb = sized_total + 2  # 2MB GPT overhead

    # Log summary
    parts_desc = " + ".join(
        f"{p['size_mb']}MB {p['label']}" for p in part_info
    )
    content_desc = " + ".join(
        f"{p['content_mb']}MB" for p in part_info
    )
    cfg.log(f"  GPT image: {total_mb}MB total ({parts_desc}, "
            f"{content_desc} content)")

    # Windows (and anywhere without root/loop devices): pure-Python GPT writer.
    # No diskpart, no WSL, no sgdisk/loop. FAT32 partitions only.
    if _is_windows():
        _build_gpt_pure_python(cfg, part_info, output)
        actual_size = os.path.getsize(output)
        part_names = "+".join(str(p["label"]) for p in part_info)
        cfg.log(f"  [OK] Created {output} ({actual_size // (1024*1024)}MB, "
                f"GPT+{part_names})")
        if cfg.verify:
            _verify_write(cfg, source_files, output)
        return

    # Native path (Linux/macOS with root): sgdisk + loop devices + mkfs.
    _check_root(cfg, "GPT image creation")
    ensure_tools(cfg, "gpt")

    with tempfile.TemporaryDirectory() as staging:
        # Stage files for each partition
        staging_dirs: list[str] = []
        for i, info in enumerate(part_info):
            pdir = Path(staging) / f"part{i}"
            pdir.mkdir()
            files = info["files"]
            if files:
                _stage_files(files, pdir)  # type: ignore[arg-type]
            staging_dirs.append(_resolve(str(pdir)))

        # Create sparse image
        _run(cfg, ["dd", "if=/dev/zero", f"of={out}", "bs=1M",
                   "count=0", f"seek={total_mb}"], verbose=cfg.verbose)

        # GPT partition table
        _run(cfg, ["sgdisk", "-Z", out], verbose=cfg.verbose)
        _run(cfg, ["sgdisk", "-o", out], verbose=cfg.verbose)

        # Create partitions
        for i, info in enumerate(part_info):
            pnum = i + 1
            size_mb = int(info["size_mb"])  # type: ignore[arg-type]
            label = str(info["label"])
            sgdisk_type = str(info["sgdisk_type"])

            if size_mb == 0 or (i == len(part_info) - 1 and size_mb > 0):
                # Last partition or "rest of disk" -- use 0:0 for last
                if size_mb == 0:
                    size_spec = "0:0"
                else:
                    size_spec = f"+{size_mb}M" if i < len(part_info) - 1 else "0:0"
            else:
                size_spec = f"+{size_mb}M"

            if pnum == 1:
                start = "2048"
            else:
                start = "0"

            _run(cfg, ["sgdisk",
                       "-n", f"{pnum}:{start}:{size_spec}",
                       "-t", f"{pnum}:{sgdisk_type}",
                       "-c", f"{pnum}:{label}",
                       out], verbose=True)

        loop_dev = _setup_loop_device(cfg, out)
        try:
            for i, info in enumerate(part_info):
                pnum = i + 1
                label = str(info["label"])
                fs_type = str(info["fs_type"])
                files = info["files"]

                part_dev = _wait_for_partition(cfg, loop_dev, pnum)

                cfg.log(f"  Formatting partition {pnum} ({label})...")
                spec = info["spec"]
                cs = spec.cluster_size if hasattr(spec, 'cluster_size') else 0  # type: ignore[union-attr]
                _format_partition(cfg, part_dev, fs_type, label, cs)

                if files:
                    cfg.log(f"  Copying {len(files)} files to partition {pnum}...")  # type: ignore[arg-type]
                    _populate_partition(cfg, staging_dirs[i], part_dev)
        finally:
            _teardown_loop_device(cfg, loop_dev)

    actual_size = os.path.getsize(output)
    part_names = "+".join(str(p["label"]) for p in part_info)
    cfg.log(f"  [OK] Created {output} ({actual_size // (1024*1024)}MB, "
            f"GPT+{part_names})")

    if cfg.verify:
        _verify_write(cfg, source_files, output)
