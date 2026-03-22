#!/usr/bin/env python3
"""
mkimage — Create bootable UEFI media images from a directory.

Generates FAT32 disk images (.img) or ISO images (.iso) containing
UEFI applications. Runs natively on Linux/macOS or via WSL on Windows.

Usage:
    # From a directory of .efi files:
    python mkimage.py build/binaries/ -o ipmitool.img
    python mkimage.py build/binaries/ -o ipmitool.iso

    # Include extra files:
    python mkimage.py build/binaries/ --include scripts/xfer-server.py -o ipmitool.img

    # Set volume label:
    python mkimage.py build/binaries/ -o ipmitool.img --label IPMITOOL

    # Specify image size (MB, for .img only):
    python mkimage.py build/binaries/ -o ipmitool.img --size 64

    # Write an image to a USB flash drive:
    python mkimage.py --write-usb ipmitool.img

Requirements:
    - Linux/macOS: mtools (mcopy, mmd, mkfs.vfat), xorriso or genisoimage
    - Windows: WSL with the above tools installed

Python 3.7+ — no external packages required.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# WSL bridge
# ---------------------------------------------------------------------------

def _is_windows() -> bool:
    return platform.system() == "Windows"


def _wsl_path(win_path: str) -> str:
    """Convert a Windows path to a WSL /mnt/c/... path."""
    p = Path(win_path).resolve()
    drive = p.drive.rstrip(":").lower()
    rest = str(p).replace(p.drive, "").replace("\\", "/")
    return f"/mnt/{drive}{rest}"


# Module-level log function and verbose flag
_log_fn = print
_verbose = False


def _set_verbose(v: bool) -> None:
    """Set module-level verbose flag."""
    global _verbose
    _verbose = v


def _get_verbose() -> bool:
    """Get module-level verbose flag."""
    return _verbose


_verify = False


def _set_verify(v: bool) -> None:
    """Set module-level verify flag."""
    global _verify
    _verify = v


def _get_verify() -> bool:
    """Get module-level verify flag."""
    return _verify


_gpt = False


def _set_gpt(v: bool) -> None:
    """Set module-level GPT flag."""
    global _gpt
    _gpt = v


def _get_gpt() -> bool:
    """Get module-level GPT flag."""
    return _gpt


def _set_log(fn: object) -> None:
    """Set the module-level log function (used by GUI to redirect output)."""
    global _log_fn
    _log_fn = fn


def _run(cmd: list[str], check: bool = True, verbose: bool = False,
         as_root: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """Run a command, routing through WSL on Windows.

    Args:
        as_root: Run as root. On Windows uses 'wsl -u root'. On Linux uses 'sudo'.
    """
    if _is_windows():
        shell_cmd = " ".join(_shell_quote(c) for c in cmd)
        if as_root:
            actual = ["wsl", "-u", "root", "bash", "-c", shell_cmd]
        else:
            actual = ["wsl", "bash", "-c", shell_cmd]
    else:
        if as_root:
            actual = ["sudo"] + cmd
        else:
            actual = cmd
    if verbose:
        prefix = "(root) " if as_root else ""
        _log_fn(f"  > {prefix}{' '.join(cmd)}")
    result = subprocess.run(actual, check=check, capture_output=True, text=True, **kwargs)
    if verbose and result.stdout.strip():
        for line in result.stdout.strip().splitlines():
            _log_fn(f"  {line}")
    if verbose and result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            _log_fn(f"  {line}")
    return result


def _shell_quote(s: str) -> str:
    """Shell-quote a string for bash."""
    if all(c.isalnum() or c in "-_./=:" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def _which(tool: str) -> bool:
    """Check if a tool is available (via WSL on Windows)."""
    try:
        r = _run(["which", tool], check=False)
        return r.returncode == 0
    except FileNotFoundError:
        return False


def _resolve(path: str) -> str:
    """Resolve a path for the execution environment (WSL or native)."""
    if _is_windows():
        return _wsl_path(path)
    return str(Path(path).resolve())


# ---------------------------------------------------------------------------
# Tool checks and auto-install
# ---------------------------------------------------------------------------

# Map tool names to packages per distro family
_TOOL_PACKAGES: dict[str, dict[str, str]] = {
    "mkfs.vfat": {"apt": "dosfstools",  "dnf": "dosfstools",  "pacman": "dosfstools"},
    "mcopy":     {"apt": "mtools",      "dnf": "mtools",      "pacman": "mtools"},
    "mmd":       {"apt": "mtools",      "dnf": "mtools",      "pacman": "mtools"},
    "rsync":     {"apt": "rsync",       "dnf": "rsync",       "pacman": "rsync"},
    "xorriso":   {"apt": "xorriso",     "dnf": "xorriso",     "pacman": "libisoburn"},
    "genisoimage": {"apt": "genisoimage", "dnf": "genisoimage", "pacman": "cdrtools"},
}


def _detect_pkg_manager() -> tuple[str, str]:
    """Detect the package manager. Returns (command, name)."""
    for cmd, name in [("apt-get", "apt"), ("dnf", "dnf"), ("yum", "dnf"), ("pacman", "pacman")]:
        if _which(cmd):
            return cmd, name
    return "", ""


def _install_packages(packages: list[str]) -> bool:
    """Attempt to install packages via the system package manager."""
    pkg_cmd, pkg_name = _detect_pkg_manager()
    if not pkg_cmd:
        return False

    # Deduplicate
    pkgs = sorted(set(packages))
    print(f"  Installing: {' '.join(pkgs)} (via {pkg_cmd})")

    if pkg_name == "pacman":
        cmd = [pkg_cmd, "-S", "--noconfirm"] + pkgs
    else:
        cmd = [pkg_cmd, "install", "-y"] + pkgs

    try:
        r = _run(cmd, check=False, verbose=True, as_root=True)
        if r.returncode != 0:
            _log_fn(f"  Install failed: {r.stderr.strip()}")
            return False
        return True
    except Exception as e:
        print(f"  Install failed: {e}", file=sys.stderr)
        return False


def _resolve_packages(tools: list[str]) -> list[str]:
    """Map missing tool names to installable package names."""
    _, pkg_name = _detect_pkg_manager()
    if not pkg_name:
        return tools
    pkgs = []
    for tool in tools:
        if tool in _TOOL_PACKAGES and pkg_name in _TOOL_PACKAGES[tool]:
            pkgs.append(_TOOL_PACKAGES[tool][pkg_name])
        else:
            pkgs.append(tool)
    return sorted(set(pkgs))


def check_tools_img() -> list[str]:
    """Check tools needed for FAT32 .img creation. Returns missing tools."""
    missing = []
    for tool in ["dd", "mkfs.vfat", "rsync"]:
        if not _which(tool):
            missing.append(tool)
    return missing


def check_tools_iso() -> list[str]:
    """Check tools needed for ISO creation. Returns missing tools."""
    if _which("xorriso"):
        return []
    if _which("genisoimage"):
        return []
    return ["xorriso"]


def ensure_tools(format: str) -> None:
    """Check for required tools and auto-install if missing."""
    if format == "img":
        missing = check_tools_img()
    else:
        missing = check_tools_iso()

    if not missing:
        return

    print(f"  Missing tools: {', '.join(missing)}")
    packages = _resolve_packages(missing)

    print(f"  Attempting auto-install...")
    if _install_packages(packages):
        # Verify installation worked
        if format == "img":
            still_missing = check_tools_img()
        else:
            still_missing = check_tools_iso()
        if not still_missing:
            print(f"  Tools installed successfully.")
            return

    # Failed — give manual instructions
    pkg_cmd, _ = _detect_pkg_manager()
    if _is_windows():
        print(f"Error: missing tools. Run in WSL:", file=sys.stderr)
        print(f"    sudo apt install {' '.join(packages)}", file=sys.stderr)
    elif pkg_cmd:
        print(f"Error: auto-install failed. Run manually:", file=sys.stderr)
        print(f"    sudo {pkg_cmd} install {' '.join(packages)}", file=sys.stderr)
    else:
        print(f"Error: missing tools: {', '.join(missing)}", file=sys.stderr)
        print(f"    Install packages: {' '.join(packages)}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

def collect_files(source_dir: str, includes: list[str]) -> dict[str, str]:
    """Collect files to include in the image.

    Returns a dict of {image_path: local_path}. image_path uses forward
    slashes and is relative to the image root.
    """
    files: dict[str, str] = {}
    src = Path(source_dir)

    if not src.is_dir():
        print(f"Error: {source_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Recursively add all files from source directory
    for p in sorted(src.rglob("*")):
        if p.is_file():
            rel = str(PurePosixPath(p.relative_to(src)))
            files[rel] = str(p.resolve())

    # Add extra include files
    for inc in includes:
        p = Path(inc)
        if not p.exists():
            print(f"Warning: --include {inc} not found, skipping", file=sys.stderr)
            continue
        if p.is_file():
            files[p.name] = str(p.resolve())
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    rel = str(PurePosixPath(f.relative_to(p)))
                    files[rel] = str(f.resolve())

    return files


# ---------------------------------------------------------------------------
# FAT32 .img builder
# ---------------------------------------------------------------------------

def build_img(files: dict[str, str], output: str, label: str, extra_mb: int = 32) -> None:
    """Create a FAT32 disk image.

    Stages files into a temp directory, creates the image in a location
    where loop mount works (native /tmp, not NTFS), mounts it, rsyncs
    files in, then copies the result to the output path.
    Falls back to mcopy if mount/rsync is not available.

    Args:
        extra_mb: Extra free space beyond content size (default 32MB).
    """
    ensure_tools("img")

    out = _resolve(output)

    # Auto-size: content + extra free space
    total_bytes = sum(os.path.getsize(lp) for lp in files.values())
    content_mb = max(1, -(-total_bytes // (1024 * 1024)))  # ceiling division
    # FAT32 minimum is ~36MB usable; VHD/partition overhead needs extra, so 40MB floor
    size_mb = max(40, content_mb + extra_mb)
    _log_fn(f"  Image size: {size_mb}MB ({content_mb}MB content + {size_mb - content_mb}MB free)")

    # Stage files preserving directory structure (native Python I/O)
    with tempfile.TemporaryDirectory() as staging:
        stg = Path(staging)
        for img_path, local_path in files.items():
            dest = stg / img_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, dest)

        stg_resolved = _resolve(staging)

        if _verbose:
            rsync_flags = "-av --no-owner --no-group"
        else:
            rsync_flags = "-a --no-owner --no-group --info=progress2"

        # Create image in a location where loop mount works:
        # - Linux: output path is fine (native filesystem)
        # - Windows/WSL: create in WSL /tmp, then copy to output
        _log_fn(f"  Copying {len(files)} files to image...")
        if _is_windows():
            # Build in WSL /tmp, rsync from /mnt/c staged dir, copy result
            r = _run([
                "bash", "-c",
                f"set -e && "
                f"IMG=$(mktemp /tmp/mkimage.XXXXXX.img) && "
                f"dd if=/dev/zero of=$IMG bs=1M count={size_mb} 2>/dev/null && "
                f"mkfs.vfat -F 32 -n '{label[:11]}' $IMG >/dev/null && "
                f"MNTDIR=$(mktemp -d) && "
                f"mount -o loop $IMG $MNTDIR && "
                f"rsync {rsync_flags} '{stg_resolved}'/ $MNTDIR/ && "
                f"TOTAL=$(find $MNTDIR -type f | wc -l) && "
                f"echo \"Copied $TOTAL files\" && "
                f"umount $MNTDIR && "
                f"rmdir $MNTDIR && "
                f"cp $IMG '{out}' && "
                f"rm -f $IMG"
            ], check=False, verbose=True, as_root=True)
        else:
            # Linux: create image at output path, mount directly
            dd_cmd = ["dd", "if=/dev/zero", f"of={out}", "bs=1M", f"count={size_mb}"]
            if _verbose:
                dd_cmd.append("status=progress")
            _run(dd_cmd, verbose=True)
            _run(["mkfs.vfat", "-F", "32", "-n", label[:11], out], verbose=True)

            r = _run([
                "bash", "-c",
                f"MNTDIR=$(mktemp -d) && "
                f"mount -o loop '{out}' $MNTDIR && "
                f"rsync {rsync_flags} '{stg_resolved}'/ $MNTDIR/ && "
                f"TOTAL=$(find $MNTDIR -type f | wc -l) && "
                f"echo \"Copied $TOTAL files\" && "
                f"umount $MNTDIR && "
                f"rmdir $MNTDIR"
            ], check=False, verbose=True, as_root=True)

        if r.returncode != 0:
            # Fallback to mcopy (no root needed, works without mount)
            _log_fn("  mount/rsync failed, falling back to mcopy...")
            if not _which("mcopy"):
                _log_fn("  Installing mtools for mcopy fallback...")
                _install_packages(_resolve_packages(["mcopy", "mmd"]))

            if _is_windows():
                # Need to create the image via WSL since dd/mkfs weren't run
                _run(["dd", "if=/dev/zero", f"of={out}", "bs=1M",
                      f"count={size_mb}"], verbose=True)
                _run(["mkfs.vfat", "-F", "32", "-n", label[:11], out],
                     verbose=True)

            dirs_created: set[str] = set()
            for img_path in sorted(files.keys()):
                parts = PurePosixPath(img_path).parts
                for i in range(1, len(parts)):
                    d = str(PurePosixPath(*parts[:i]))
                    if d not in dirs_created:
                        _run(["mmd", "-i", out, f"::{d}"], check=False)
                        dirs_created.add(d)
            for img_path, local_path in sorted(files.items()):
                src = _resolve(local_path)
                _run(["mcopy", "-i", out, src, f"::{img_path}"],
                     verbose=_verbose)
            _log_fn(f"  Copied {len(files)} files via mcopy")

    actual_size = os.path.getsize(output)
    _log_fn(f"  [OK] Created {output} ({actual_size // 1024}KB, FAT32)")


# ---------------------------------------------------------------------------
# ISO builder
# ---------------------------------------------------------------------------

def build_iso(files: dict[str, str], output: str, label: str) -> None:
    """Create an ISO image."""
    ensure_tools("iso")

    # Stage files in a temp directory
    with tempfile.TemporaryDirectory() as staging:
        stg = Path(staging)
        for img_path, local_path in files.items():
            dest = stg / img_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, dest)

        stg_resolved = _resolve(staging)
        out = _resolve(output)

        if _which("xorriso"):
            _run([
                "xorriso", "-as", "mkisofs",
                "-o", out,
                "-R", "-J", "-joliet-long",
                "-V", label[:32],
                stg_resolved,
            ], verbose=True)
        else:
            _run([
                "genisoimage",
                "-o", out,
                "-R", "-J", "-joliet-long",
                "-V", label[:32],
                stg_resolved,
            ], verbose=True)

    actual_size = os.path.getsize(output)
    print(f"  Created {output} ({actual_size // 1024}KB, ISO 9660)")


# ---------------------------------------------------------------------------
# USB write
# ---------------------------------------------------------------------------

MAX_USB_SIZE_GB = 256


def _list_removable_drives_linux() -> list[dict[str, str]]:
    """List removable drives on Linux via lsblk."""
    drives = []
    r = _run(["lsblk", "-d", "-n", "-o", "NAME,SIZE,RM,TYPE,MODEL,TRAN",
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

        mr = _run(["lsblk", "-n", "-o", "MOUNTPOINT", dev_path], check=False)
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
    drives = []
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


def _list_removable_drives() -> list[dict[str, str]]:
    """List removable USB drives. Auto-detects platform."""
    if _is_windows():
        return _list_removable_drives_windows()
    return _list_removable_drives_linux()


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


def write_usb(
    image_path: str,
    source_dir: str = "",
    includes: Optional[list[str]] = None,
    label: str = "UEFITOOLS",
    select_drive: Optional[object] = None,
    confirm_write: Optional[object] = None,
    log: Callable[..., None] = print,
) -> None:
    """Write files to a USB drive with safety checks.

    On Windows: uses diskpart to format USB as FAT32, then copies files directly.
    On Linux: writes a pre-built .img file via dd.

    Args:
        image_path: Path to .img file (used on Linux for dd, or as fallback on Windows).
        source_dir: Source directory containing files to copy (Windows diskpart path).
        includes: Extra files/directories to include (Windows diskpart path).
        label: Volume label for FAT32 format.
        select_drive: Callback(drives_list) -> drive_dict or None. Defaults to CLI input().
        confirm_write: Callback(target_dict) -> bool. Defaults to CLI input().
        log: Output function. Defaults to print.
    """
    if select_drive is None:
        select_drive = _cli_select_drive
    if confirm_write is None:
        confirm_write = _cli_confirm_write

    log("Scanning for removable drives...")
    drives = _list_removable_drives()

    if not drives:
        log("No removable USB drives found.")
        log("  - On Windows/WSL, USB passthrough may need usbipd")
        log(f"  - Drive must be removable and under {MAX_USB_SIZE_GB}GB")
        return

    log(f"Found {len(drives)} removable drive(s)")
    target = select_drive(drives)
    if target is None:
        log("Aborted.")
        return

    target_path = target["path"]
    target_size = int(target["size_bytes"])

    # Safety checks
    if "sda" in target["name"] and not _is_windows():
        mr = _run(["lsblk", "-n", "-o", "MOUNTPOINT", target_path], check=False)
        mounts = mr.stdout.strip() if mr.returncode == 0 else ""
        if "/" in mounts or "/boot" in mounts or "/home" in mounts:
            log(f"Error: {target_path} has system partitions mounted. Refusing.")
            return

    if target_size > MAX_USB_SIZE_GB * (1024 ** 3):
        log(f"Error: {target_path} is larger than {MAX_USB_SIZE_GB}GB. Refusing.")
        return

    if not confirm_write(target):
        log("Aborted.")
        return

    if _is_windows():
        _write_usb_windows(source_dir, includes or [], label, target, log)
    else:
        if not os.path.isfile(image_path):
            log(f"Error: {image_path} not found")
            return
        img_resolved = _resolve(image_path)
        img_size = os.path.getsize(image_path)
        _write_usb_linux(image_path, img_size, img_resolved, target, log)


def _write_usb_windows(
    source_dir: str, includes: list[str], label: str,
    target: dict[str, str], log: object
) -> None:
    """Format USB as FAT32 via diskpart and copy files directly.

    Uses an elevated PowerShell subprocess for the diskpart + copy.
    No raw disk write — just format and file copy.
    """
    target_path = target["path"]  # \\.\PhysicalDriveN
    disk_num = target_path.rsplit("PhysicalDrive", 1)[-1]
    label_trim = label[:11]

    log(f"Formatting disk {disk_num} as FAT32 and copying files...")

    fd, progress_file = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    fd, script_file = tempfile.mkstemp(suffix=".ps1")
    os.close(fd)

    prg_esc = progress_file.replace("'", "''")

    # Build source paths list for the elevated script
    sources: list[str] = []
    if source_dir and os.path.isdir(source_dir):
        sources.append(source_dir)
    for inc in includes:
        if inc and os.path.exists(inc):
            sources.append(inc)
    sources_str = ",".join(f"'{s.replace(chr(39), chr(39)*2)}'" for s in sources)
    verbose_str = "$true" if _get_verbose() else "$false"
    verify_str = "$true" if _get_verify() else "$false"
    gpt_str = "$true" if _get_gpt() else "$false"

    ps_script = f"""
try {{
    "Preparing USB drive (disk {disk_num})... [native Windows, no WSL]" | Out-File -Append '{prg_esc}'

    $useGpt = {gpt_str}
    $partStyle = if ($useGpt) {{ "GPT" }} else {{ "MBR" }}

    # Step 1: Take disk offline
    "  Taking disk offline..." | Out-File -Append '{prg_esc}'
    Set-Disk -Number {disk_num} -IsOffline $true -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1

    # Step 2: clean (separate session)
    "  diskpart: clean..." | Out-File -Append '{prg_esc}'
    ("select disk {disk_num}`r`nclean" | diskpart 2>&1) | Out-Null
    Start-Sleep -Seconds 1

    # Step 3: convert (separate session, ignore failure if already correct type)
    "  diskpart: convert $partStyle..." | Out-File -Append '{prg_esc}'
    ("select disk {disk_num}`r`nconvert $($partStyle.ToLower())" | diskpart 2>&1) | Out-Null
    Start-Sleep -Seconds 1

    # Step 4: create + format (separate session)
    if ($useGpt) {{
        $dpCreate = @"
select disk {disk_num}
create partition primary
select partition 1
format fs=fat32 quick label={label_trim}
"@
    }} else {{
        $dpCreate = @"
select disk {disk_num}
create partition primary
active
format fs=fat32 quick label={label_trim}
"@
    }}
    "  diskpart: create + format..." | Out-File -Append '{prg_esc}'
    $dpOut = ($dpCreate | diskpart 2>&1) | Out-String
    $dpSummary = $dpOut.Trim() -replace '[\r\n]+', ' | '
    "  diskpart: $dpSummary" | Out-File -Append '{prg_esc}'
    if ($dpOut -notmatch "successfully formatted") {{
        throw "diskpart failed. Output: $dpSummary"
    }}

    # Step 5: Disable automount, bring online
    "  Disabling automount, bringing disk online..." | Out-File -Append '{prg_esc}'
    "automount disable" | diskpart 2>&1 | Out-Null
    Set-Disk -Number {disk_num} -IsOffline $false -ErrorAction SilentlyContinue
    Set-Disk -Number {disk_num} -IsReadOnly $false -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2

    # Step 6: Get or assign drive letter
    $part = Get-Partition -DiskNumber {disk_num} -ErrorAction SilentlyContinue |
        Where-Object {{ $_.Type -ne 'Reserved' -and $_.Type -ne 'System' }} |
        Select-Object -First 1
    if (-not $part) {{ throw "No partition found after diskpart" }}

    $driveLetter = $part.DriveLetter
    if ($driveLetter -and $driveLetter -ne "`0") {{
        "  Partition already has drive letter ${{driveLetter}}:" | Out-File -Append '{prg_esc}'
    }} else {{
        $used = @((Get-CimInstance Win32_LogicalDisk).DeviceID -replace ':', '')
        $driveLetter = (69..90 | ForEach-Object {{ [char]$_ }} |
            Where-Object {{ "$_" -notin $used }} | Select-Object -First 1)
        if (-not $driveLetter) {{ throw "No free drive letters" }}
        "  Add-PartitionAccessPath ${{driveLetter}}:\" | Out-File -Append '{prg_esc}'
        $part | Add-PartitionAccessPath -AccessPath "${{driveLetter}}:\" -ErrorAction Stop
    }}
    Start-Sleep -Seconds 3

    $destRoot = "${{driveLetter}}:\"
    if (Test-Path $destRoot) {{
        "  Drive ${{driveLetter}}: accessible (FAT32)" | Out-File -Append '{prg_esc}'
    }} else {{
        throw "Drive ${{driveLetter}}: not accessible after mount"
    }}

    $sources = @({sources_str})
    $verbose = {verbose_str}
    $totalFiles = 0
    foreach ($s in $sources) {{
        if (-not $s) {{ continue }}
        if (Test-Path $s -PathType Leaf) {{ $totalFiles++ }}
        elseif (Test-Path $s -PathType Container) {{
            $totalFiles += (Get-ChildItem -LiteralPath $s -Recurse -File).Count
        }}
    }}
    "Copying $totalFiles files from $($sources.Count) source path(s) to $destRoot [robocopy]..." | Out-File -Append '{prg_esc}'
    $totalCopied = 0
    $totalFailed = 0

    foreach ($src in $sources) {{
        if (-not $src) {{ continue }}
        if (Test-Path $src -PathType Leaf) {{
            $fileName = Split-Path $src -Leaf
            $srcDir = Split-Path $src -Parent
            "  Copying file: $fileName" | Out-File -Append '{prg_esc}'
            robocopy $srcDir $destRoot $fileName /NJH /NJS /NP 2>&1 | Out-Null
            $rcExit = $LASTEXITCODE
            if ($rcExit -lt 8) {{ $totalCopied++ }} else {{
                "  WARN: robocopy exit $rcExit for $fileName" | Out-File -Append '{prg_esc}'
                $totalFailed++
            }}
        }} elseif (Test-Path $src -PathType Container) {{
            $srcNorm = $src.TrimEnd('\', '/')
            "Copying directory: $srcNorm -> $destRoot" | Out-File -Append '{prg_esc}'
            if ($verbose) {{
                $rcOut = robocopy $srcNorm $destRoot /S /E /NP /NJH /NJS 2>&1
            }} else {{
                $rcOut = robocopy $srcNorm $destRoot /S /E /NP /NFL /NDL /NJH /NJS 2>&1
            }}
            $rcExit = $LASTEXITCODE
            $rcText = ($rcOut | Out-String).Trim()
            if ($verbose -and $rcText) {{
                "ROBOCOPY output:`r`n$rcText" | Out-File -Append '{prg_esc}'
            }}
            if ($rcExit -lt 8) {{
                $fileCount = (Get-ChildItem -LiteralPath $srcNorm -Recurse -File).Count
                $totalCopied += $fileCount
                "Copied $fileCount files (robocopy exit $rcExit)" | Out-File -Append '{prg_esc}'
            }} else {{
                "robocopy FAILED for $srcNorm (exit $rcExit)`r`nOutput: $rcText" | Out-File -Append '{prg_esc}'
                $totalFailed++
            }}
        }}
    }}

    if (Test-Path "${{driveLetter}}:\") {{
        $fileCount = (Get-ChildItem "${{driveLetter}}:\" -Recurse -File -ErrorAction SilentlyContinue).Count
        "  Drive ${{driveLetter}}: accessible, $fileCount files on disk" | Out-File -Append '{prg_esc}'
    }} else {{
        "  WARNING: Drive ${{driveLetter}}: not accessible" | Out-File -Append '{prg_esc}'
    }}

    # Verify files if requested
    $verify = {verify_str}
    $verifyFailed = 0
    if ($verify -and $totalCopied -gt 0) {{
        "Verifying $totalCopied files [Get-FileHash MD5]..." | Out-File -Append '{prg_esc}'
        foreach ($src in $sources) {{
            if (-not $src) {{ continue }}
            if (Test-Path $src -PathType Leaf) {{
                $fileName = Split-Path $src -Leaf
                $destFile = Join-Path $destRoot $fileName
                $srcHash = (Get-FileHash -LiteralPath $src -Algorithm MD5).Hash
                if (Test-Path $destFile) {{
                    $dstHash = (Get-FileHash -LiteralPath $destFile -Algorithm MD5).Hash
                    if ($srcHash -ne $dstHash) {{
                        "  VERIFY FAIL: $fileName (hash mismatch)" | Out-File -Append '{prg_esc}'
                        $verifyFailed++
                    }} else {{
                        if ($verbose) {{ "  VERIFY OK: $fileName" | Out-File -Append '{prg_esc}' }}
                    }}
                }} else {{
                    "  VERIFY FAIL: $fileName (missing on USB)" | Out-File -Append '{prg_esc}'
                    $verifyFailed++
                }}
            }} elseif (Test-Path $src -PathType Container) {{
                $srcNorm = $src.TrimEnd('\', '/')
                $files = Get-ChildItem -LiteralPath $srcNorm -Recurse -File
                foreach ($f in $files) {{
                    $relPath = $f.FullName.Substring($srcNorm.Length + 1)
                    $destFile = Join-Path $destRoot $relPath
                    if (Test-Path $destFile) {{
                        $srcHash = (Get-FileHash -LiteralPath $f.FullName -Algorithm MD5).Hash
                        $dstHash = (Get-FileHash -LiteralPath $destFile -Algorithm MD5).Hash
                        if ($srcHash -ne $dstHash) {{
                            "  VERIFY FAIL: $relPath (hash mismatch)" | Out-File -Append '{prg_esc}'
                            $verifyFailed++
                        }} else {{
                            if ($verbose) {{ "  VERIFY OK: $relPath" | Out-File -Append '{prg_esc}' }}
                        }}
                    }} else {{
                        "  VERIFY FAIL: $relPath (missing on USB)" | Out-File -Append '{prg_esc}'
                        $verifyFailed++
                    }}
                }}
            }}
        }}
        if ($verifyFailed -eq 0) {{
            "Verification passed: all $totalCopied files match" | Out-File -Append '{prg_esc}'
        }} else {{
            "Verification FAILED: $verifyFailed file(s) differ" | Out-File -Append '{prg_esc}'
        }}
    }}

    if ($totalFailed -gt 0 -or $verifyFailed -gt 0) {{
        "OK:${{totalCopied}}:WARN:${{totalFailed}} copy errors, ${{verifyFailed}} verify errors" | Out-File -Append '{prg_esc}'
    }} else {{
        "OK:$totalCopied" | Out-File -Append '{prg_esc}'
    }}
}} catch {{
    "ERROR: $_" | Out-File -Append '{prg_esc}'
}}
"""
    with open(script_file, "w") as f:
        f.write(ps_script)

    try:
        log("  Requesting Administrator access...")
        proc = subprocess.Popen([
            "powershell.exe", "-NoProfile", "-Command",
            f"Start-Process -FilePath 'powershell.exe' "
            f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','{script_file}' "
            f"-Verb RunAs -Wait"
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        import time
        lines_read = 0
        while proc.poll() is None:
            time.sleep(0.3)
            if os.path.exists(progress_file):
                try:
                    with open(progress_file, "r", encoding="utf-8", errors="replace") as pf:
                        all_lines = pf.readlines()
                    if len(all_lines) > lines_read:
                        for line in all_lines[lines_read:]:
                            stripped = line.rstrip()
                            if stripped:
                                log(f"  {stripped}")
                        lines_read = len(all_lines)
                except OSError:
                    pass

        # Read remaining lines after process exits
        time.sleep(0.5)
        final_msg = ""
        if os.path.exists(progress_file):
            try:
                with open(progress_file, "r", encoding="utf-8", errors="replace") as pf:
                    all_lines = pf.readlines()
                if len(all_lines) > lines_read:
                    for line in all_lines[lines_read:]:
                        stripped = line.rstrip()
                        if stripped:
                            log(f"  {stripped}")
                final_msg = all_lines[-1].strip() if all_lines else ""
            except OSError:
                pass

        if final_msg.startswith("OK:"):
            count = final_msg.split(":")[1]
            log(f"[OK] Wrote {count} files to USB drive. "
                f"You can safely remove it.")
        elif final_msg.startswith("ERROR:"):
            log(f"[ERROR] {final_msg}")
        elif final_msg:
            log(f"  {final_msg}")
        else:
            log("[ERROR] Write subprocess produced no output. UAC may have been denied.")

    except Exception as exc:
        log(f"[ERROR] {exc}")
    finally:
        for f in (script_file, progress_file):
            try:
                os.unlink(f)
            except OSError:
                pass


def _write_usb_linux(
    image_path: str, img_size: int, img_resolved: str,
    target: dict[str, str], log: object
) -> None:
    """Write image to USB on Linux using dd."""
    target_path = target["path"]

    # Unmount any mounted partitions
    log(f"Unmounting {target_path} partitions...")
    _run(["umount", f"{target_path}*"], check=False)
    for i in range(1, 10):
        _run(["umount", f"{target_path}{i}"], check=False)
        _run(["umount", f"{target_path}p{i}"], check=False)

    log(f"Writing {image_path} ({img_size // (1024*1024)}MB) to {target_path}...")
    dd_cmd = ["dd", f"if={img_resolved}", f"of={target_path}", "bs=4M", "conv=fsync"]
    if _verbose:
        dd_cmd.insert(-1, "status=progress")
    r = _run(dd_cmd, check=False, verbose=True)

    if r.returncode != 0:
        log("  Retrying as root...")
        r = _run([
            "dd", f"if={img_resolved}", f"of={target_path}",
            "bs=4M", "conv=fsync",
        ], check=False, verbose=True, as_root=True)

    if r.returncode != 0:
        log(f"[ERROR] dd failed: {r.stderr.strip()}")
        log("  You may need to run with sudo.")
        return

    _run(["sync"], check=False)
    log(f"[OK] Wrote {img_size // (1024*1024)}MB to {target_path}. "
        f"You can safely remove the USB drive.")


# ---------------------------------------------------------------------------
# Tkinter GUI
# ---------------------------------------------------------------------------

def gui_main() -> None:
    """Launch the Tkinter graphical interface."""
    import queue
    import threading
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext
    except ImportError:
        print("Error: tkinter not available.", file=sys.stderr)
        print("  Linux:   sudo dnf install python3-tkinter  (or apt: python3-tk)", file=sys.stderr)
        print("  Windows: tkinter is included with Python", file=sys.stderr)
        sys.exit(1)

    log_queue: queue.Queue = queue.Queue()

    def log(msg: str) -> None:
        log_queue.put(msg + "\n")

    def poll_log() -> None:
        while not log_queue.empty():
            msg = log_queue.get_nowait()
            log_text.insert(tk.END, msg)
            log_text.see(tk.END)
        root.after(100, poll_log)

    def browse_source() -> None:
        d = filedialog.askdirectory(title="Select Source Directory")
        if d:
            source_var.set(d)

    def add_include_file() -> None:
        f = filedialog.askopenfilename(title="Select File to Include")
        if f:
            includes_list.insert(tk.END, f)

    def add_include_dir() -> None:
        d = filedialog.askdirectory(title="Select Directory to Include")
        if d:
            includes_list.insert(tk.END, d)

    def clear_includes() -> None:
        includes_list.delete(0, tk.END)

    def browse_output() -> None:
        ext = ".img" if fmt_var.get() == "img" else ".iso"
        f = filedialog.asksaveasfilename(
            title="Save Image As",
            defaultextension=ext,
            filetypes=[(f"{'FAT32' if ext == '.img' else 'ISO'} Image", f"*{ext}"), ("All Files", "*.*")],
        )
        if f:
            output_var.set(f)

    def on_format_change(*_args) -> None:
        state = tk.NORMAL if fmt_var.get() == "img" else tk.DISABLED
        size_entry.config(state=state)

    def refresh_usb_drives() -> None:
        """Refresh the USB drive dropdown."""
        drives = _list_removable_drives()
        usb_drives.clear()
        usb_drives.extend(drives)
        menu = drive_combo["menu"]
        menu.delete(0, tk.END)
        if not drives:
            menu.add_command(label="(no USB drives found)", command=lambda: drive_var.set(""))
            drive_var.set("")
        else:
            for d in drives:
                model = f"  {d['model']}" if d['model'] else ""
                label_text = f"{d['path']}  {d['size']}{model}"
                menu.add_command(label=label_text, command=lambda v=label_text: drive_var.set(v))
            drive_var.set(f"{drives[0]['path']}  {drives[0]['size']}"
                         f"{'  ' + drives[0]['model'] if drives[0]['model'] else ''}")

    def on_usb_toggle() -> None:
        """Toggle between file output and USB drive output."""
        if usb_var.get():
            # Switch to USB mode
            output_entry.grid_remove()
            browse_btn.grid_remove()
            drive_frame.grid(row=6, column=1, columnspan=3, sticky=tk.EW, padx=10, pady=2)
            create_btn.config(text="Write to Target")
            target_label.config(text="Output Target:")
            refresh_usb_drives()
        else:
            # Switch to file mode
            drive_frame.grid_remove()
            output_entry.grid(row=6, column=1, columnspan=2, sticky=tk.EW, padx=10, pady=2)
            browse_btn.grid(row=6, column=3, padx=10, pady=2)
            create_btn.config(text="Create Image")
            target_label.config(text="Output Target:")

    def gui_confirm_write(target: dict[str, str]) -> bool:
        return messagebox.askyesno(
            "Confirm Write",
            f"WARNING: ALL DATA on {target['path']} "
            f"({target['size']} {target['model']}) WILL BE DESTROYED.\n\n"
            f"Are you sure you want to write to {target['path']}?",
            icon=messagebox.WARNING,
        )

    def do_create() -> None:
        src = source_var.get().strip()
        if not src:
            messagebox.showerror("Error", "Source directory is required.")
            return

        to_usb = usb_var.get()
        if to_usb:
            # Find the selected drive
            sel = drive_var.get().strip()
            target_drive = None
            for d in usb_drives:
                if d["path"] in sel:
                    target_drive = d
                    break
            if not target_drive:
                messagebox.showerror("Error", "No USB drive selected.\nClick Refresh if no drives appear.")
                return
        else:
            out = output_var.get().strip()
            if not out:
                messagebox.showerror("Error", "Output file is required.")
                return

        includes = list(includes_list.get(0, tk.END))
        label = label_var.get().strip() or "UEFITOOLS"
        size_mb = int(size_var.get()) if size_var.get().isdigit() else 32
        fmt = fmt_var.get()

        create_btn.config(state=tk.DISABLED)

        def run() -> None:
            _set_log(log)
            _set_verbose(verbose_var.get())
            _set_verify(verify_var.get())
            _set_gpt(gpt_var.get())
            try:
                log(f"Collecting files from {src}...")
                files = collect_files(src, includes)
                if not files:
                    log("Error: no files found.")
                    return
                log(f"  {len(files)} files")
                for p in sorted(files.keys()):
                    log(f"    {p}")

                if to_usb:
                    if not gui_confirm_write(target_drive):
                        log("Aborted.")
                        return

                    if _is_windows():
                        # Windows: diskpart format + copy directly, no image needed
                        log(f"Writing to {target_drive['path']}...")
                        write_usb(
                            "",
                            source_dir=src,
                            includes=includes,
                            label=label,
                            select_drive=lambda _drives: target_drive,
                            confirm_write=lambda _t: True,
                            log=log,
                        )
                    else:
                        # Linux: build temp image, then dd to USB
                        import tempfile as _tf
                        with _tf.NamedTemporaryFile(suffix=".img", delete=False) as tmp:
                            tmp_path = tmp.name
                        try:
                            log("Building FAT32 image...")
                            ensure_tools("img")
                            build_img(files, tmp_path, label, size_mb)

                            log(f"Writing to {target_drive['path']}...")
                            write_usb(
                                tmp_path,
                                select_drive=lambda _drives: target_drive,
                                confirm_write=lambda _t: True,
                                log=log,
                            )
                        finally:
                            try:
                                os.unlink(tmp_path)
                            except OSError:
                                pass
                else:
                    ext = Path(out).suffix.lower()
                    if ext == ".img" or (ext != ".iso" and fmt == "img"):
                        log("Building FAT32 image...")
                        ensure_tools("img")
                        build_img(files, out, label, size_mb)
                    else:
                        log("Building ISO image...")
                        ensure_tools("iso")
                        build_iso(files, out, label)
                    log("Done.")
            except Exception as e:
                log(f"Error: {e}")
            finally:
                create_btn.config(state=tk.NORMAL)

        threading.Thread(target=run, daemon=True).start()

    # --- Build the window ---
    root = tk.Tk()
    root.title("mkimage \u2014 Bootable Media Creator")
    root.resizable(False, False)

    pad = {"padx": 10, "pady": 2}
    usb_drives: list[dict[str, str]] = []

    # Source directory
    tk.Label(root, text="Source Directory:").grid(row=0, column=0, sticky=tk.W, **pad)
    source_var = tk.StringVar()
    tk.Entry(root, textvariable=source_var, width=50).grid(row=0, column=1, columnspan=2, sticky=tk.EW, **pad)
    tk.Button(root, text="Browse...", command=browse_source).grid(row=0, column=3, **pad)

    # Extra includes
    tk.Label(root, text="Extra Includes:").grid(row=1, column=0, sticky=tk.NW, **pad)
    inc_btn_frame = tk.Frame(root)
    inc_btn_frame.grid(row=1, column=1, columnspan=2, sticky=tk.W, **pad)
    tk.Button(inc_btn_frame, text="Add File", command=add_include_file).pack(side=tk.LEFT, padx=(0, 5))
    tk.Button(inc_btn_frame, text="Add Dir", command=add_include_dir).pack(side=tk.LEFT, padx=(0, 5))
    tk.Button(inc_btn_frame, text="Clear", command=clear_includes).pack(side=tk.LEFT)

    includes_list = tk.Listbox(root, height=4, width=60)
    includes_list.grid(row=2, column=0, columnspan=4, sticky=tk.EW, padx=10, pady=2)

    # Output format
    tk.Label(root, text="Output Format:").grid(row=3, column=0, sticky=tk.W, **pad)
    fmt_var = tk.StringVar(value="img")
    fmt_frame = tk.Frame(root)
    fmt_frame.grid(row=3, column=1, columnspan=2, sticky=tk.W, **pad)
    tk.Radiobutton(fmt_frame, text="FAT32 (.img)", variable=fmt_var, value="img").pack(side=tk.LEFT)
    tk.Radiobutton(fmt_frame, text="ISO (.iso)", variable=fmt_var, value="iso").pack(side=tk.LEFT, padx=10)
    fmt_var.trace_add("write", on_format_change)

    # Volume label
    tk.Label(root, text="Volume Label:").grid(row=4, column=0, sticky=tk.W, **pad)
    label_var = tk.StringVar(value="UEFITOOLS")
    tk.Entry(root, textvariable=label_var, width=20).grid(row=4, column=1, sticky=tk.W, **pad)

    # Image size
    tk.Label(root, text="Extra Space (MB):").grid(row=5, column=0, sticky=tk.W, **pad)
    size_var = tk.StringVar(value="32")
    size_entry = tk.Entry(root, textvariable=size_var, width=10)
    size_entry.grid(row=5, column=1, sticky=tk.W, **pad)

    # Write to USB toggle
    usb_var = tk.BooleanVar(value=False)
    verbose_var = tk.BooleanVar(value=False)
    verify_var = tk.BooleanVar(value=False)
    gpt_var = tk.BooleanVar(value=False)
    opt_frame = tk.Frame(root)
    opt_frame.grid(row=5, column=2, columnspan=2, sticky=tk.E, **pad)
    tk.Checkbutton(opt_frame, text="Verbose", variable=verbose_var).pack(side=tk.LEFT, padx=(0, 8))
    tk.Checkbutton(opt_frame, text="Verify", variable=verify_var).pack(side=tk.LEFT, padx=(0, 8))
    tk.Checkbutton(opt_frame, text="GPT", variable=gpt_var).pack(side=tk.LEFT, padx=(0, 8))
    tk.Checkbutton(opt_frame, text="Write to USB", variable=usb_var,
                   command=on_usb_toggle).pack(side=tk.LEFT)

    # Output target — file entry (default) or drive dropdown (USB mode)
    target_label = tk.Label(root, text="Output Target:")
    target_label.grid(row=6, column=0, sticky=tk.W, **pad)

    output_var = tk.StringVar()
    output_entry = tk.Entry(root, textvariable=output_var, width=50)
    output_entry.grid(row=6, column=1, columnspan=2, sticky=tk.EW, **pad)
    browse_btn = tk.Button(root, text="Browse...", command=browse_output)
    browse_btn.grid(row=6, column=3, **pad)

    # USB drive dropdown (hidden by default)
    drive_frame = tk.Frame(root)
    drive_var = tk.StringVar(value="")
    drive_combo = tk.OptionMenu(drive_frame, drive_var, "")
    drive_combo.config(width=45, anchor=tk.W, font=("Consolas", 9))
    drive_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
    tk.Button(drive_frame, text="Refresh", command=refresh_usb_drives).pack(side=tk.LEFT, padx=(5, 0))

    # Action button (single button, label changes)
    action_frame = tk.Frame(root)
    action_frame.grid(row=7, column=0, columnspan=4, pady=10)
    create_btn = tk.Button(action_frame, text="Create Image", width=20, command=do_create)
    create_btn.pack()

    # Log
    tk.Label(root, text="Log:").grid(row=8, column=0, sticky=tk.NW, **pad)
    log_text = scrolledtext.ScrolledText(root, height=10, width=70, state=tk.NORMAL, font=("Consolas", 9))
    log_text.grid(row=9, column=0, columnspan=4, sticky=tk.EW, padx=10, pady=(0, 10))
    log_text.insert(tk.END, "Ready.\n")

    root.after(100, poll_log)
    root.mainloop()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create bootable UEFI media images from a directory.",
        epilog="""\
examples:
  %(prog)s build/binaries/ -o ipmitool.img
  %(prog)s build/binaries/ -o ipmitool.iso --label IPMITOOL
  %(prog)s . --include scripts/xfer-server.py -o tools.img
  %(prog)s --write-usb ipmitool.img
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "source_dir", nargs="?",
        help="Directory to include (recursively)",
    )
    parser.add_argument(
        "-o", "--output",
        help="Output file (.img for FAT32, .iso for ISO)",
    )
    parser.add_argument(
        "--include", action="append", default=[],
        help="Additional file or directory to include (repeatable)",
    )
    parser.add_argument(
        "--label", default="UEFITOOLS",
        help="Volume label (default: UEFITOOLS)",
    )
    parser.add_argument(
        "--extra", type=int, default=32, dest="extra_mb",
        help="Extra free space in MB beyond content size (default: 32)",
    )
    parser.add_argument(
        "--write-usb", metavar="IMAGE",
        help="Write an existing .img to a USB drive (lists removable drives)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show per-file output and transfer progress",
    )
    parser.add_argument(
        "--gui", action="store_true",
        help="Launch graphical interface",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Check tool availability and exit",
    )

    args = parser.parse_args()

    if args.gui or len(sys.argv) == 1:
        gui_main()
        return

    if hasattr(args, 'verbose') and args.verbose:
        _set_verbose(True)

    if args.write_usb:
        write_usb(args.write_usb)
        return

    if args.check:
        img_missing = check_tools_img()
        iso_missing = check_tools_iso()
        env = "WSL" if _is_windows() else "native"
        print(f"Environment: {env}")
        print(f"FAT32 (.img): {'OK' if not img_missing else 'MISSING: ' + ', '.join(img_missing)}")
        print(f"ISO   (.iso): {'OK' if not iso_missing else 'MISSING: ' + ', '.join(iso_missing)}")
        if img_missing or iso_missing:
            all_missing = sorted(set(img_missing + iso_missing))
            packages = _resolve_packages(all_missing)
            pkg_cmd, _ = _detect_pkg_manager()
            if pkg_cmd:
                print(f"\nInstall all: sudo {pkg_cmd} install {' '.join(packages)}")
        sys.exit(1 if (img_missing or iso_missing) else 0)

    if not args.source_dir:
        parser.error("source_dir is required")
    if not args.output:
        parser.error("-o/--output is required")

    ext = Path(args.output).suffix.lower()
    if ext not in (".img", ".iso"):
        print(f"Error: output must be .img or .iso, got '{ext}'", file=sys.stderr)
        sys.exit(1)

    print(f"Collecting files from {args.source_dir}...")
    files = collect_files(args.source_dir, args.include)
    if not files:
        print("Error: no files found", file=sys.stderr)
        sys.exit(1)

    print(f"  {len(files)} files, {sum(os.path.getsize(p) for p in files.values()) // 1024}KB total")
    for img_path in sorted(files.keys()):
        print(f"    {img_path}")

    print(f"Building {ext.lstrip('.')} image...")
    if ext == ".img":
        build_img(files, args.output, args.label, args.extra_mb)
    else:
        build_iso(files, args.output, args.label)

    print("Done.")


if __name__ == "__main__":
    main()
