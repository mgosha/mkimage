"""GPT-partitioned image builders (single ESP and ESP + data)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from mkimage.files import _calculate_content_size, _parse_size, _stage_files
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
    from mkimage import Config


def build_gpt_img(cfg: Config, files: dict[str, str], output: str) -> None:
    """Create a GPT disk image with a single EFI System Partition."""
    _check_root(cfg, "GPT image creation")
    ensure_tools(cfg, "gpt")
    out = _resolve(output)

    content_mb = _calculate_content_size(files)
    esp_mb = max(int(content_mb * 1.3 + 10), 64)
    total_mb = esp_mb + 2  # 2MB GPT overhead
    esp_label = cfg.esp_label[:11]
    cfg.log(f"  GPT image: {total_mb}MB total ({esp_mb}MB ESP, "
            f"{content_mb}MB content)")

    with tempfile.TemporaryDirectory() as staging:
        _stage_files(files, Path(staging))
        stg_resolved = _resolve(staging)

        # Create sparse image
        _run(cfg, ["dd", "if=/dev/zero", f"of={out}", "bs=1M",
                   "count=0", f"seek={total_mb}"], verbose=cfg.verbose)

        # GPT partition table
        _run(cfg, ["sgdisk", "-Z", out], verbose=cfg.verbose)
        _run(cfg, ["sgdisk", "-o", out], verbose=cfg.verbose)
        _run(cfg, ["sgdisk",
                   "-n", f"1:2048:+{esp_mb}M",
                   "-t", "1:EF00",
                   "-c", f"1:{esp_label}",
                   out], verbose=True)

        loop_dev = _setup_loop_device(cfg, out)
        try:
            esp_part = _wait_for_partition(cfg, loop_dev, 1)

            cfg.log(f"  Formatting ESP ({esp_label})...")
            _format_partition(cfg, esp_part, cfg.fs_type, esp_label)

            cfg.log(f"  Copying {len(files)} files to ESP...")
            _populate_partition(cfg, stg_resolved, esp_part)
        finally:
            _teardown_loop_device(cfg, loop_dev)

    actual_size = os.path.getsize(output)
    cfg.log(f"  [OK] Created {output} ({actual_size // (1024*1024)}MB, "
            f"GPT+ESP)")

    if cfg.verify:
        _verify_write(cfg, files, output)


def build_gpt_data_img(cfg: Config, esp_files: dict[str, str],
                       data_files: dict[str, str], output: str) -> None:
    """Create a GPT disk image with ESP + data partition."""
    _check_root(cfg, "GPT image creation")
    ensure_tools(cfg, "gpt")
    out = _resolve(output)

    esp_content_mb = _calculate_content_size(esp_files)
    esp_mb = max(int(esp_content_mb * 1.3 + 10), 64)

    data_content_mb = _calculate_content_size(data_files) if data_files else 1
    data_mb = (_parse_size(cfg.data_size) if cfg.data_size
               else max(int(data_content_mb * 1.3 + 10), 34))

    total_mb = esp_mb + data_mb + 2  # 2MB GPT overhead
    esp_label = cfg.esp_label[:11]
    data_label = cfg.data_label[:11]
    cfg.log(f"  GPT image: {total_mb}MB total "
            f"({esp_mb}MB ESP + {data_mb}MB data, "
            f"{esp_content_mb}MB + {data_content_mb}MB content)")

    with tempfile.TemporaryDirectory() as staging:
        esp_staging = Path(staging) / "esp"
        data_staging = Path(staging) / "data"
        esp_staging.mkdir()
        _stage_files(esp_files, esp_staging)
        data_staging.mkdir()
        if data_files:
            _stage_files(data_files, data_staging)

        esp_stg = _resolve(str(esp_staging))
        data_stg = _resolve(str(data_staging))

        # Create sparse image
        _run(cfg, ["dd", "if=/dev/zero", f"of={out}", "bs=1M",
                   "count=0", f"seek={total_mb}"], verbose=cfg.verbose)

        # GPT partition table with two partitions
        _run(cfg, ["sgdisk", "-Z", out], verbose=cfg.verbose)
        _run(cfg, ["sgdisk", "-o", out], verbose=cfg.verbose)
        _run(cfg, ["sgdisk",
                   "-n", f"1:2048:+{esp_mb}M",
                   "-t", "1:EF00",
                   "-c", f"1:{esp_label}",
                   out], verbose=True)
        _run(cfg, ["sgdisk",
                   "-n", "2:0:0",
                   "-t", "2:0700",
                   "-c", f"2:{data_label}",
                   out], verbose=True)

        loop_dev = _setup_loop_device(cfg, out)
        try:
            esp_part = _wait_for_partition(cfg, loop_dev, 1)
            data_part = _wait_for_partition(cfg, loop_dev, 2)

            # Format and populate ESP
            cfg.log(f"  Formatting ESP ({esp_label})...")
            _format_partition(cfg, esp_part, cfg.fs_type, esp_label)
            cfg.log(f"  Copying {len(esp_files)} files to ESP...")
            _populate_partition(cfg, esp_stg, esp_part)

            # Format and populate data
            cfg.log(f"  Formatting data ({data_label})...")
            _format_partition(cfg, data_part, cfg.fs_type, data_label)
            if data_files:
                cfg.log(f"  Copying {len(data_files)} files to data...")
                _populate_partition(cfg, data_stg, data_part)
        finally:
            _teardown_loop_device(cfg, loop_dev)

    actual_size = os.path.getsize(output)
    cfg.log(f"  [OK] Created {output} ({actual_size // (1024*1024)}MB, "
            f"GPT+ESP+DATA)")
