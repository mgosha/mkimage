"""Partition operations: formatting, loop devices, root checks."""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from mkimage.platform import _find_tool, _is_macos, _is_windows, _run

if TYPE_CHECKING:
    from mkimage import Config


def _format_partition(cfg: Config, device: str, fs_type: str,
                      label: str, cluster_size: int = 0) -> None:
    """Format a partition with the specified filesystem.

    Args:
        fs_type: 'fat32', 'exfat', 'ext4', 'ntfs', or 'udf'.
        cluster_size: Cluster/block size in bytes (0 = auto/default).
    """
    label = label[:11]  # FAT32/exFAT label limit
    mac = _is_macos()
    if fs_type == "fat32":
        if mac:
            cmd = ["newfs_msdos", "-F", "32", "-v", label]
        else:
            cmd = [_find_tool("mkfs.vfat"), "-F", "32", "-n", label]
            if cluster_size > 0:
                cmd.extend(["-s", str(cluster_size // 512)])
        cmd.append(device)
        _run(cfg, cmd, verbose=True, as_root=True)
    elif fs_type == "exfat":
        if mac:
            cmd = ["newfs_exfat", "-v", label]
        else:
            cmd = [_find_tool("mkfs.exfat"), "-n", label]
            if cluster_size > 0:
                cmd.extend(["-s", str(cluster_size)])
        cmd.append(device)
        _run(cfg, cmd, verbose=True, as_root=True)
    elif fs_type == "ext4":
        cmd = [_find_tool("mkfs.ext4"), "-L", label, "-F"]
        if cluster_size > 0:
            cmd.extend(["-b", str(cluster_size)])
        cmd.append(device)
        _run(cfg, cmd, verbose=True, as_root=True)
    elif fs_type == "ntfs":
        cmd = [_find_tool("mkfs.ntfs"), "-f", "-L", label]
        if cluster_size > 0:
            cmd.extend(["-c", str(cluster_size)])
        cmd.append(device)
        _run(cfg, cmd, verbose=True, as_root=True)
    elif fs_type == "udf":
        if _is_macos():
            cmd = ["newfs_udf", "-v", label, device]
        else:
            cmd = [_find_tool("mkudffs"), "--label", label, "--vid", label, device]
        _run(cfg, cmd, verbose=True, as_root=True)
    else:
        raise ValueError(f"Unknown filesystem type: {fs_type}")


def _setup_loop_device(cfg: Config, image_path: str) -> str:
    """Attach image to a loop/disk device with partition scanning.

    On Linux: uses losetup. Returns /dev/loopN.
    On macOS: uses hdiutil attach. Returns /dev/diskN.
    Raises RuntimeError on failure.
    """
    if _is_macos():
        r = _run(cfg, [
            "hdiutil", "attach", "-nomount",
            "-imagekey", "diskimage-class=CRawDiskImage",
            image_path,
        ], verbose=True, as_root=True)
        # hdiutil outputs lines like "/dev/disk4  GUID_partition_scheme"
        for line in r.stdout.strip().splitlines():
            parts = line.split()
            if parts and parts[0].startswith("/dev/disk"):
                dev = parts[0]
                # Return the base disk device (not partition)
                if "s" not in dev.split("disk")[-1]:
                    return dev
        # Fallback: first /dev/disk* token
        for line in r.stdout.strip().splitlines():
            for token in line.split():
                if token.startswith("/dev/disk"):
                    return token
        raise RuntimeError(f"hdiutil attach returned no device: {r.stdout}")

    r = _run(cfg, [
        "losetup", "--find", "--show", "--partscan", image_path
    ], verbose=True, as_root=True)
    loop_dev = r.stdout.strip()
    if not loop_dev:
        raise RuntimeError("losetup returned no device")
    return loop_dev


def _wait_for_partition(cfg: Config, loop_dev: str,
                        part_num: int, retries: int = 10) -> str:
    """Wait for a partition device to appear.

    On Linux: /dev/loopNpM. On macOS: /dev/diskNsM.
    Raises RuntimeError if it doesn't appear within retries.
    """
    import time
    if _is_macos():
        part_path = f"{loop_dev}s{part_num}"
    else:
        part_path = f"{loop_dev}p{part_num}"
    for _ in range(retries):
        r = _run(cfg, ["test", "-b", part_path], check=False, as_root=True)
        if r.returncode == 0:
            return part_path
        if not _is_macos():
            _run(cfg, ["partprobe", loop_dev], check=False, as_root=True)
        time.sleep(0.5)
    raise RuntimeError(
        f"Partition {part_path} did not appear after {retries} retries"
    )


def _teardown_loop_device(cfg: Config, loop_dev: str) -> None:
    """Detach a loop/disk device."""
    if _is_macos():
        _run(cfg, ["hdiutil", "detach", loop_dev], check=False, as_root=True)
    else:
        _run(cfg, ["losetup", "-d", loop_dev], check=False, as_root=True)


def _check_root(cfg: Config, operation: str) -> None:
    """Check if we have root access. Raises RuntimeError if not.

    On Linux, checks euid. On Windows, assumes WSL has root via wsl -u root.
    """
    if _is_windows():
        return  # WSL uses wsl -u root, no check needed
    if os.geteuid() != 0:
        detail = "hdiutil + mount" if _is_macos() else "losetup + mount"
        raise RuntimeError(
            f"{operation} requires root ({detail}). Run with sudo."
        )


def _populate_partition(cfg: Config, staging_dir: str,
                        partition: str) -> None:
    """Mount a partition, rsync staged files into it, unmount.

    Raises RuntimeError on failure.
    """
    if cfg.verbose:
        rsync_flags = "-av --no-owner --no-group"
    else:
        rsync_flags = "-a --no-owner --no-group --info=progress2"

    r = _run(cfg, [
        "bash", "-c",
        f"MNTDIR=$(mktemp -d) && "
        f"mount '{partition}' $MNTDIR && "
        f"rsync {rsync_flags} '{staging_dir}'/ $MNTDIR/ && "
        f"sync && "
        f"umount $MNTDIR && "
        f"rmdir $MNTDIR"
    ], check=False, verbose=True, as_root=True)

    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to populate {partition}: {r.stderr.strip()}"
        )
