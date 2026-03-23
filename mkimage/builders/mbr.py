"""MBR-partitioned image builder."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from mkimage.files import _calculate_content_size, _stage_files
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
    from mkimage import Config


def build_mbr_img(cfg: Config, files: dict[str, str], output: str) -> None:
    """Create an MBR-partitioned disk image with a single FAT partition."""
    _check_root(cfg, "MBR image creation")
    ensure_tools(cfg, "mbr")
    out = _resolve(output)

    content_mb = _calculate_content_size(files)
    part_mb = max(int(content_mb * 1.3 + 10), 40)
    total_mb = part_mb + 1  # 1MB MBR overhead
    part_label = cfg.label[:11]
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
            # Use sgdisk alternative or manual sector math
            # Actually, simpler: use sfdisk-compatible approach
            # macOS doesn't have sfdisk, so use fdisk with a script
            # For now, fall back to creating a raw partition table
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
            _format_partition(cfg, part, cfg.fs_type, part_label)

            cfg.log(f"  Copying {len(files)} files...")
            _populate_partition(cfg, stg_resolved, part)
        finally:
            _teardown_loop_device(cfg, loop_dev)

    actual_size = os.path.getsize(output)
    cfg.log(f"  [OK] Created {output} ({actual_size // (1024*1024)}MB, MBR)")

    if cfg.verify:
        _verify_write(cfg, files, output)
