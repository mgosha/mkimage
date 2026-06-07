"""MBR-partitioned image builder."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from mkimage.files import _calculate_content_size, _interpret_size, _stage_files
from mkimage.partition import (
    _check_root,
    _format_partition,
    _populate_partition,
    _setup_loop_device,
    _teardown_loop_device,
    _wait_for_partition,
)
from mkimage.platform import _is_macos, _resolve, _run
from mkimage.tools import ensure_tools
from mkimage.verify import _verify_write

if TYPE_CHECKING:
    from mkimage import Config, PartitionSpec


def build_mbr_img(cfg: Config, files: dict[str, str], output: str) -> None:
    """Create an MBR-partitioned disk image with a single FAT partition.

    On Windows (or when root is not available), uses the pure Python
    FAT32 writer with MBR support — no admin or external tools needed.
    """
    from mkimage import PartitionSpec
    from mkimage.platform import _is_windows

    # Pure Python path: works on all platforms, no root needed
    # Use native tools only when root is available and fs_type needs them
    from mkimage.builders.img import _build_img_windows, _write_fat32_image
    from mkimage.builders.img import _calculate_content_size, _interpret_size

    part_spec_check = cfg.partitions[0] if cfg.partitions else PartitionSpec()
    fs = part_spec_check.fs_type if part_spec_check.fs_type != "esp" else "fat32"

    # Pure Python supports FAT32 only; other filesystems need native tools
    if fs == "fat32" or _is_windows():
        if fs != "fat32":
            # The pure-Python writer only does FAT32. Don't silently mislead
            # the caller into thinking they got exFAT/NTFS/etc.
            cfg.log(f"  Warning: '{fs}' filesystem is not supported by the "
                    f"pure-Python image writer (used on Windows / without "
                    f"root); creating FAT32 instead.")
        part = cfg.partitions[0] if cfg.partitions else PartitionSpec()
        content_mb = _calculate_content_size(files)
        size_mb = _interpret_size(part.size, content_mb)
        cfg.log(f"  Image size: {size_mb}MB ({content_mb}MB content + {size_mb - content_mb}MB free)")
        cfg.log(f"  {len(files)} files ({content_mb * 1024}KB) to include")
        _write_fat32_image(cfg, files, output, size_mb, part.label[:11],
                           part.cluster_size, mbr=True)
        actual_size = os.path.getsize(output)
        cfg.log(f"  [OK] Created {output} ({actual_size // (1024*1024)}MB, MBR FAT32)")
        if cfg.verify:
            _verify_write(cfg, files, output)
        return

    # Non-FAT32 on Linux/macOS: need native tools + root
    _check_root(cfg, "MBR image creation")
    ensure_tools(cfg, "mbr")
    out = _resolve(output)

    part_spec = cfg.partitions[0] if cfg.partitions else PartitionSpec()
    content_mb = _calculate_content_size(files)
    part_mb = _interpret_size(part_spec.size, content_mb)
    total_mb = part_mb + 1  # 1MB MBR overhead
    part_label = part_spec.label[:11]
    fs_type = part_spec.fs_type if part_spec.fs_type != "esp" else "fat32"
    cfg.log(f"  MBR image: {total_mb}MB total ({part_mb}MB partition, "
            f"{content_mb}MB content)")

    with tempfile.TemporaryDirectory() as staging:
        _stage_files(files, Path(staging))
        stg_resolved = _resolve(staging)

        # Create image
        _run(cfg, ["dd", "if=/dev/zero", f"of={out}", "bs=1M",
                   "count=0", f"seek={total_mb}"], verbose=cfg.verbose)

        # Create MBR partition table with single partition
        if _is_macos():
            # macOS fdisk: create single partition
            _run(cfg, [
                "bash", "-c",
                f"echo 'y' | fdisk -i -a dos '{out}'"
            ], check=False, verbose=cfg.verbose)
            _run(cfg, [
                "bash", "-c",
                f"printf ',,0x0C,*\\n' | sfdisk '{out}'"
            ], check=False, verbose=cfg.verbose)
        else:
            # Linux: sfdisk is scriptable
            _run(cfg, [
                "bash", "-c",
                f"echo ',,0x0C,*' | sfdisk '{out}'"
            ], verbose=True)

        loop_dev = _setup_loop_device(cfg, out)
        try:
            part = _wait_for_partition(cfg, loop_dev, 1)

            cfg.log(f"  Formatting partition ({part_label})...")
            _format_partition(cfg, part, fs_type, part_label, part_spec.cluster_size)

            cfg.log(f"  Copying {len(files)} files...")
            _populate_partition(cfg, stg_resolved, part)
        finally:
            _teardown_loop_device(cfg, loop_dev)

    actual_size = os.path.getsize(output)
    cfg.log(f"  [OK] Created {output} ({actual_size // (1024*1024)}MB, MBR)")

    if cfg.verify:
        _verify_write(cfg, files, output)
