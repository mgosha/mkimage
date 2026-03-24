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
    if _is_windows():
        from mkimage.builders.img import _build_img_windows
        _build_img_windows(cfg, files, output, mbr=True)
        return

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
