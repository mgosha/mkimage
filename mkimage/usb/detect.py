"""USB drive detection across platforms."""
from __future__ import annotations

import re
import subprocess

from mkimage import Config
from mkimage.platform import _is_macos, _is_windows, _run

MAX_USB_SIZE_GB = 2048


def _list_removable_drives_linux() -> list[dict[str, str]]:
    """List removable drives on Linux via lsblk."""
    drives: list[dict[str, str]] = []
    quiet = Config()
    r = _run(quiet, ["lsblk", "-d", "-n", "-o", "NAME,SIZE,RM,TYPE,MODEL,TRAN",
              "--bytes"], check=False)
    if r.returncode != 0:
        return drives

    for line in r.stdout.strip().splitlines():
        parts = line.split(None, 5)
        if len(parts) < 4:
            continue
        name, size_bytes_str, removable, dtype = parts[0], parts[1], parts[2], parts[3]
        model = parts[4] if len(parts) > 4 else ""
        transport = parts[5].strip() if len(parts) > 5 else ""

        if dtype != "disk":
            continue
        if removable != "1" and "usb" not in transport.lower():
            continue

        dev_path = f"/dev/{name}"

        mr = _run(quiet, ["lsblk", "-n", "-o", "MOUNTPOINT", dev_path], check=False)
        mountpoints = mr.stdout.strip().splitlines() if mr.returncode == 0 else []
        if any(mp.strip() == "/" for mp in mountpoints):
            continue

        try:
            size_bytes = int(size_bytes_str)
        except ValueError:
            continue

        size_gb = size_bytes / (1024 ** 3)
        if size_gb > MAX_USB_SIZE_GB:
            continue

        size_str = f"{size_gb:.1f}GB" if size_gb >= 1 else f"{size_bytes // (1024 * 1024)}MB"
        drives.append({
            "name": name, "path": dev_path, "size": size_str,
            "size_bytes": str(size_bytes), "model": model.strip(),
        })
    return drives


def _list_removable_drives_windows() -> list[dict[str, str]]:
    """List removable USB drives on Windows via PowerShell Get-Disk."""
    drives: list[dict[str, str]] = []
    ps_cmd = (
        "Get-Disk | Where-Object { $_.BusType -eq 'USB' } | "
        "ForEach-Object { "
        "  $num = $_.Number; $size = $_.Size; $model = $_.FriendlyName; "
        "  $hasC = (Get-Partition -DiskNumber $num -ErrorAction SilentlyContinue | "
        "    Where-Object { $_.DriveLetter -eq 'C' }).Count -gt 0; "
        "  if (-not $hasC) { "
        "    Write-Output \"$num|$size|$model\" "
        "  } "
        "}"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            return drives

        for line in r.stdout.strip().splitlines():
            parts = line.strip().split("|", 2)
            if len(parts) < 3:
                continue
            num_str, size_str, model = parts
            try:
                disk_num = int(num_str)
                size_bytes = int(size_str)
            except ValueError:
                continue

            size_gb = size_bytes / (1024 ** 3)
            if size_gb > MAX_USB_SIZE_GB:
                continue

            size_display = f"{size_gb:.1f}GB" if size_gb >= 1 else f"{size_bytes // (1024 * 1024)}MB"
            drives.append({
                "name": f"disk{disk_num}",
                "path": f"\\\\.\\PhysicalDrive{disk_num}",
                "size": size_display,
                "size_bytes": str(size_bytes),
                "model": model.strip(),
            })
    except FileNotFoundError:
        pass
    return drives


def _list_removable_drives_macos() -> list[dict[str, str]]:
    """List removable USB drives on macOS via diskutil."""
    drives: list[dict[str, str]] = []
    quiet = Config()
    # Get external/removable disks
    r = _run(quiet, ["diskutil", "list", "external"], check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return drives

    # Parse diskutil list output for /dev/diskN lines
    for line in r.stdout.splitlines():
        m = re.match(r"^(/dev/disk\d+)\s+\(external", line)
        if not m:
            continue
        dev_path = m.group(1)

        # Get details via diskutil info
        info_r = _run(quiet, ["diskutil", "info", dev_path], check=False)
        if info_r.returncode != 0:
            continue

        info = info_r.stdout
        size_bytes = 0
        model = ""
        for info_line in info.splitlines():
            info_line = info_line.strip()
            if info_line.startswith("Disk Size:"):
                # "Disk Size:  15728640000 Bytes ..."
                size_match = re.search(r"(\d+)\s+Bytes", info_line)
                if size_match:
                    size_bytes = int(size_match.group(1))
            elif info_line.startswith("Device / Media Name:"):
                model = info_line.split(":", 1)[1].strip()

        if size_bytes == 0:
            continue

        size_gb = size_bytes / (1024 ** 3)
        if size_gb > MAX_USB_SIZE_GB:
            continue

        name = dev_path.replace("/dev/", "")
        size_str = f"{size_gb:.1f}GB" if size_gb >= 1 else f"{size_bytes // (1024 * 1024)}MB"
        drives.append({
            "name": name, "path": dev_path, "size": size_str,
            "size_bytes": str(size_bytes), "model": model,
        })

    return drives


def _list_removable_drives() -> list[dict[str, str]]:
    """List removable USB drives. Auto-detects platform."""
    if _is_windows():
        return _list_removable_drives_windows()
    if _is_macos():
        return _list_removable_drives_macos()
    return _list_removable_drives_linux()
