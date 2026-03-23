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


def build_gpt_img(cfg: Config, source_files: dict[str, str],
                   output: str) -> None:
    """Create a GPT disk image with N partitions from cfg.partitions."""
    from mkimage import PartitionSpec

    _check_root(cfg, "GPT image creation")
    ensure_tools(cfg, "gpt")
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
                _format_partition(cfg, part_dev, fs_type, label)

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
