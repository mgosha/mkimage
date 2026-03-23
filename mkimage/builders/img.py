"""FAT32 .img builder (no partition table)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from mkimage.files import _calculate_content_size, _interpret_size, _stage_files
from mkimage.platform import _is_windows, _resolve, _run, _which
from mkimage.tools import _suggest_install, ensure_tools
from mkimage.verify import _verify_write

if TYPE_CHECKING:
    from mkimage import Config, PartitionSpec


def _populate_img_mcopy(cfg: Config, files: dict[str, str],
                        out: str) -> None:
    """Populate a FAT32 image using mcopy/mmd (no root needed)."""
    dirs_created: set[str] = set()
    for img_path in sorted(files.keys()):
        parts = PurePosixPath(img_path).parts
        for i in range(1, len(parts)):
            d = str(PurePosixPath(*parts[:i]))
            if d not in dirs_created:
                _run(cfg, ["mmd", "-i", out, f"::{d}"], check=False)
                dirs_created.add(d)
    for img_path, local_path in sorted(files.items()):
        src = _resolve(local_path)
        _run(cfg, ["mcopy", "-i", out, src, f"::{img_path}"],
             verbose=cfg.verbose)
    cfg.log(f"  Copied {len(files)} files via mcopy")


def _populate_img_mount(cfg: Config, staging_dir: str, out: str,
                        size_mb: int, part_label: str) -> bool:
    """Populate a FAT32 image using mount+rsync (requires root).

    Returns True on success, False on failure.
    """
    if cfg.verbose:
        rsync_flags = "-av --no-owner --no-group"
    else:
        rsync_flags = "-a --no-owner --no-group --info=progress2"

    if _is_windows():
        r = _run(cfg, [
            "bash", "-c",
            f"set -e && "
            f"IMG=$(mktemp /tmp/mkimage.XXXXXX.img) && "
            f"dd if=/dev/zero of=$IMG bs=1M count={size_mb} 2>/dev/null && "
            f"mkfs.vfat -F 32 -n '{part_label}' $IMG >/dev/null && "
            f"MNTDIR=$(mktemp -d) && "
            f"mount -o loop $IMG $MNTDIR && "
            f"rsync {rsync_flags} '{staging_dir}'/ $MNTDIR/ && "
            f"TOTAL=$(find $MNTDIR -type f | wc -l) && "
            f"echo \"Copied $TOTAL files\" && "
            f"umount $MNTDIR && "
            f"rmdir $MNTDIR && "
            f"cp $IMG '{out}' && "
            f"rm -f $IMG"
        ], check=False, verbose=True, as_root=True)
    else:
        dd_cmd = ["dd", "if=/dev/zero", f"of={out}", "bs=1M", f"count={size_mb}"]
        if cfg.verbose:
            dd_cmd.append("status=progress")
        _run(cfg, dd_cmd, verbose=True)
        _run(cfg, ["mkfs.vfat", "-F", "32", "-n", part_label, out], verbose=True)

        r = _run(cfg, [
            "bash", "-c",
            f"MNTDIR=$(mktemp -d) && "
            f"mount -o loop '{out}' $MNTDIR && "
            f"rsync {rsync_flags} '{staging_dir}'/ $MNTDIR/ && "
            f"TOTAL=$(find $MNTDIR -type f | wc -l) && "
            f"echo \"Copied $TOTAL files\" && "
            f"umount $MNTDIR && "
            f"rmdir $MNTDIR"
        ], check=False, verbose=True, as_root=True)

    return r.returncode == 0


def build_img(cfg: Config, files: dict[str, str], output: str) -> None:
    """Create a FAT32 disk image (no partition table).

    Primary method uses mcopy (no root needed). Falls back to mount+rsync
    if mcopy is unavailable and root access is available.
    """
    from mkimage import PartitionSpec

    ensure_tools(cfg, "img")
    out = _resolve(output)

    part = cfg.partitions[0] if cfg.partitions else PartitionSpec()
    content_mb = _calculate_content_size(files)
    size_mb = _interpret_size(part.size, content_mb)
    part_label = part.label[:11]
    cfg.log(f"  Image size: {size_mb}MB ({content_mb}MB content + {size_mb - content_mb}MB free)")

    with tempfile.TemporaryDirectory() as staging:
        _stage_files(files, Path(staging))
        stg_resolved = _resolve(staging)

        if _which("mcopy"):
            # Primary: mcopy (no root needed)
            cfg.log(f"  Copying {len(files)} files to image...")
            dd_cmd = ["dd", "if=/dev/zero", f"of={out}", "bs=1M", f"count={size_mb}"]
            if cfg.verbose:
                dd_cmd.append("status=progress")
            _run(cfg, dd_cmd, verbose=True)
            _run(cfg, ["mkfs.vfat", "-F", "32", "-n", part_label, out],
                 verbose=True)
            _populate_img_mcopy(cfg, files, out)
        elif _which("rsync"):
            # Fallback: mount+rsync (requires root)
            cfg.log("  mcopy not found, using mount+rsync (requires root)...")
            cfg.log(f"  Copying {len(files)} files to image...")
            if not _populate_img_mount(cfg, stg_resolved, out, size_mb, part_label):
                raise RuntimeError(
                    "mount/rsync failed. Install mtools for rootless operation:\n"
                    f"    {_suggest_install('mcopy')}"
                )
        else:
            raise RuntimeError(
                "No file copy tool available. Install mtools:\n"
                f"    {_suggest_install('mcopy')}"
            )

    actual_size = os.path.getsize(output)
    cfg.log(f"  [OK] Created {output} ({actual_size // 1024}KB, FAT32)")

    if cfg.verify:
        _verify_write(cfg, files, output)
