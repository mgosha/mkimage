"""USB safety checks, unmount, drive selection, and confirmation."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from mkimage.platform import _is_macos, _is_windows, _run, _which
from mkimage.usb.detect import MAX_USB_SIZE_GB, _list_removable_drives

if TYPE_CHECKING:
    from mkimage import Config


def _verify_usb_bus(cfg: Config, device: str) -> bool:
    """Verify a device is on the USB bus.

    Uses udevadm on Linux, diskutil on macOS.
    Returns True if USB (or if verification is unavailable).
    Returns False if the device is on a non-USB bus (SATA, NVMe, etc.).
    """
    if _is_windows():
        return True  # Windows checks bus type via Get-Disk

    if _is_macos():
        r = _run(cfg, ["diskutil", "info", device], check=False)
        if r.returncode != 0:
            return True
        for line in r.stdout.splitlines():
            if "Protocol:" in line:
                return "USB" in line
        return True  # can't determine protocol, allow

    # Linux: udevadm
    if not _which("udevadm"):
        cfg.log("  Warning: udevadm not found, skipping bus verification")
        return True
    r = _run(cfg, ["udevadm", "info", "--query=property", f"--name={device}"],
             check=False)
    if r.returncode != 0:
        return True  # can't determine, allow
    return "ID_BUS=usb" in r.stdout


def _usb_safety_checks(cfg: Config, drive: dict[str, str]) -> bool:
    """Run USB safety checks. Returns True if safe to proceed."""
    device = drive["path"]
    size_bytes = int(drive["size_bytes"])

    # Bus verification
    if not _verify_usb_bus(cfg, device):
        cfg.log(f"Error: {device} is not on the USB bus. Refusing to write.")
        cfg.log("  Use a USB-connected drive, not SATA/NVMe.")
        return False

    # System partition check
    if not _is_windows() and "sda" in drive["name"]:
        mr = _run(cfg, ["lsblk", "-n", "-o", "MOUNTPOINT", device], check=False)
        mounts = mr.stdout.strip() if mr.returncode == 0 else ""
        if "/" in mounts or "/boot" in mounts or "/home" in mounts:
            cfg.log(f"Error: {device} has system partitions mounted. Refusing.")
            return False

    # Size limit
    if size_bytes > MAX_USB_SIZE_GB * (1024 ** 3):
        cfg.log(f"Error: {device} is larger than {MAX_USB_SIZE_GB}GB. Refusing.")
        return False

    return True


def _unmount_device(cfg: Config, device: str) -> None:
    """Unmount all mounted partitions on a device."""
    cfg.log(f"  Unmounting {device} partitions...")

    if _is_macos():
        _run(cfg, ["diskutil", "unmountDisk", device], check=False)
        return

    if _which("findmnt"):
        r = _run(cfg, ["findmnt", "--list", "--noheadings", "-o", "TARGET",
                       "--source", device], check=False)
        # Also check partitions
        for suffix in [str(i) for i in range(1, 10)] + [f"p{i}" for i in range(1, 10)]:
            rp = _run(cfg, ["findmnt", "--list", "--noheadings", "-o", "TARGET",
                            "--source", f"{device}{suffix}"], check=False)
            if rp.stdout.strip():
                for mnt in rp.stdout.strip().splitlines():
                    _run(cfg, ["umount", mnt.strip()], check=False, as_root=True)
        if r.stdout.strip():
            for mnt in r.stdout.strip().splitlines():
                _run(cfg, ["umount", mnt.strip()], check=False, as_root=True)
    else:
        # Fallback: try unmounting common partition patterns
        _run(cfg, ["umount", device], check=False, as_root=True)
        for i in range(1, 10):
            _run(cfg, ["umount", f"{device}{i}"], check=False, as_root=True)
            _run(cfg, ["umount", f"{device}p{i}"], check=False, as_root=True)


def _wipe_device(cfg: Config, device: str) -> None:
    """Thoroughly wipe all partition signatures from a device.

    Removes MBR, GPT (including backup at end of disk), and filesystem
    signatures. Necessary when converting between partition schemes, as
    `diskpart clean` and `sgdisk -Z` only wipe headers, leaving stale
    backup GPT or filesystem magic bytes.
    """
    cfg.log(f"  Wiping partition signatures from {device}...")

    if _is_macos():
        # diskutil eraseDisk wipes everything
        _run(cfg, ["diskutil", "eraseDisk", "free", "EMPTY", device],
             check=False, as_root=True)
        return

    if _is_windows():
        # Handled separately in the PS1 script via diskpart clean all
        return

    # Linux: use wipefs to remove all signatures, then zero first/last 2MB
    if _which("wipefs"):
        _run(cfg, ["wipefs", "--all", "--force", device],
             check=False, as_root=True)

    # Zero first 2MB (MBR + GPT header)
    _run(cfg, ["dd", "if=/dev/zero", f"of={device}", "bs=1M", "count=2"],
         check=False, as_root=True)

    # Zero last 2MB (GPT backup header at end of disk)
    # Get disk size in bytes
    r = _run(cfg, ["blockdev", "--getsize64", device], check=False, as_root=True)
    if r.returncode == 0 and r.stdout.strip():
        try:
            disk_bytes = int(r.stdout.strip())
            # Zero last 2MB
            skip_mb = max(0, disk_bytes // (1024 * 1024) - 2)
            _run(cfg, ["dd", "if=/dev/zero", f"of={device}", "bs=1M",
                       f"seek={skip_mb}", "count=2"],
                 check=False, as_root=True)
        except ValueError:
            pass

    _run(cfg, ["sync"], check=False)


def _cli_select_drive(drives: list[dict[str, str]]) -> Optional[dict[str, str]]:
    """CLI drive selection via input()."""
    print(f"\nAvailable USB drives (removable, <={MAX_USB_SIZE_GB}GB):\n")
    for i, d in enumerate(drives):
        model = f"  {d['model']}" if d['model'] else ""
        print(f"  [{i + 1}] {d['path']}  {d['size']}{model}")
    print()
    try:
        choice = input("Select drive number (or 'q' to quit): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if choice.lower() in ("q", "quit", ""):
        return None
    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(drives):
            raise ValueError()
    except ValueError:
        print(f"Error: invalid selection '{choice}'", file=sys.stderr)
        return None
    return drives[idx]


def _cli_confirm_write(target: dict[str, str]) -> bool:
    """CLI write confirmation via input()."""
    print(f"\n  WARNING: ALL DATA on {target['path']} "
          f"({target['size']} {target['model']}) WILL BE DESTROYED.\n")
    try:
        confirm = input(f"  Type 'yes' to write to {target['path']}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    return confirm == "yes"


def _check_bad_blocks(cfg: Config, device: str) -> bool:
    """Check USB drive for bad blocks. Returns True if all blocks OK.

    Uses badblocks on Linux (destructive write test), or a dd-based
    write/read pattern on macOS. On Windows, delegates to chkdsk.

    WARNING: This is a destructive test -- all data on the device will
    be erased.
    """
    if _is_windows():
        # chkdsk needs a volume/drive letter, not a \\.\PhysicalDriveN path,
        # and must run NATIVELY (not via _run, which routes through WSL where
        # chkdsk doesn't exist -> false "bad blocks"). Resolve the disk's
        # drive letter, then run a read-only chkdsk scan against it.
        import subprocess as _sp
        disk_num = device.rsplit("PhysicalDrive", 1)[-1]
        ps = (f"Get-Partition -DiskNumber {disk_num} -ErrorAction SilentlyContinue"
              f" | Where-Object DriveLetter | Select-Object -First 1"
              f" -ExpandProperty DriveLetter")
        pr = _sp.run(["powershell", "-NoProfile", "-Command", ps],
                     capture_output=True, text=True)
        letter = (pr.stdout or "").strip()
        if not letter:
            cfg.log(f"  {device} has no mounted volume to scan "
                    f"(format it first); skipping bad-block check.")
            return True
        # Plain read-only chkdsk (no /scan: that's NTFS-only and errors on
        # FAT). Reports problems without modifying the volume.
        cfg.log(f"  Running chkdsk {letter}: (read-only) ...")
        cr = _sp.run(["chkdsk", f"{letter}:"],
                     capture_output=True, text=True)
        if cfg.verbose and (cr.stdout or cr.stderr):
            for line in ((cr.stdout or "") + (cr.stderr or "")).splitlines():
                if line.strip():
                    cfg.log(f"    {line.rstrip()}")
        return cr.returncode == 0

    if _is_macos():
        # macOS: use diskutil verifyDisk
        cfg.log(f"  Running disk verification on {device}...")
        r = _run(cfg, ["diskutil", "verifyDisk", device],
                 check=False, verbose=True)
        return r.returncode == 0

    # Linux: use badblocks if available
    if _which("badblocks"):
        cfg.log(f"  Running bad block scan on {device} (destructive write test)...")
        r = _run(cfg, ["badblocks", "-wsv", device],
                 check=False, verbose=True, as_root=True)
        return r.returncode == 0

    # Fallback: dd write pattern + read-back
    cfg.log(f"  badblocks not found, using dd write/read test on {device}...")
    cfg.log("  Writing test pattern...")
    r = _run(cfg, ["dd", "if=/dev/urandom", f"of={device}", "bs=1M",
                   "conv=fsync", "status=progress"],
             check=False, verbose=True, as_root=True)
    if r.returncode != 0:
        cfg.log(f"  Write failed: {r.stderr.strip()}")
        return False

    cfg.log("  Reading back for verification...")
    r = _run(cfg, ["dd", f"if={device}", "of=/dev/null", "bs=1M",
                   "status=progress"],
             check=False, verbose=True, as_root=True)
    if r.returncode != 0:
        cfg.log(f"  Read failed: {r.stderr.strip()}")
        return False

    return True


def _resolve_usb_target(cfg: Config, target: str,
                        select_drive: Optional[Callable[..., Optional[dict[str, str]]]] = None,
                        ) -> Optional[dict[str, str]]:
    """Resolve a USB target to a drive dict.

    If target is 'usb', auto-detect. If target is /dev/sdX, find it in
    the drive list. Returns drive dict or None if aborted.
    """
    if select_drive is None:
        select_drive = _cli_select_drive

    drives = _list_removable_drives()

    if target.lower() == "usb":
        if not drives:
            cfg.log("No removable USB drives found.")
            cfg.log("  - On Windows/WSL, USB passthrough may need usbipd")
            cfg.log(f"  - Drive must be removable and under {MAX_USB_SIZE_GB}GB")
            return None
        if len(drives) == 1:
            cfg.log(f"  Auto-detected: {drives[0]['path']} "
                    f"({drives[0]['size']} {drives[0]['model']})")
            return drives[0]
        cfg.log(f"Found {len(drives)} removable drives")
        return select_drive(drives)

    # Explicit device path -- find it in drives list or create entry
    for d in drives:
        if d["path"] == target:
            return d

    # Device not in removable list -- might be valid but not detected as removable
    cfg.log(f"Warning: {target} not found in removable drives list")
    return {"name": Path(target).name, "path": target, "size": "?",
            "size_bytes": "0", "model": ""}
