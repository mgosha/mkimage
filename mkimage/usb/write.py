"""USB write operations across platforms."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from mkimage.builders.img import build_img
from mkimage.files import (
    _calculate_content_size,
    _interpret_size,
    _stage_files,
    collect_files,
)
from mkimage.partition import _check_root, _format_partition, _populate_partition
from mkimage.platform import _is_macos, _is_windows, _resolve, _run, _which
from mkimage.tools import ensure_tools
from mkimage.usb.detect import MAX_USB_SIZE_GB, _list_removable_drives
from mkimage.usb.safety import (
    _cli_confirm_write,
    _cli_select_drive,
    _unmount_device,
    _usb_safety_checks,
    _resolve_usb_target,
    _verify_usb_bus,
    _wipe_device,
)

if TYPE_CHECKING:
    from mkimage import Config, PartitionSpec


def _is_hybrid_iso(path: str) -> bool:
    """Check if an ISO is a hybrid (has MBR boot code, can be dd'd to USB).

    Hybrid ISOs (created with isohybrid) have an MBR boot signature at
    offset 510-511 AND a valid ISO 9660 signature at offset 32769.
    """
    try:
        with open(path, 'rb') as f:
            # Check for MBR signature at offset 510-511
            f.seek(510)
            sig = f.read(2)
            if sig != b'\x55\xAA':
                return False
            # Check for ISO 9660 signature at offset 32769
            f.seek(32769)
            iso_sig = f.read(5)
            return iso_sig == b'CD001'
    except (OSError, IOError):
        return False


def _extract_iso_to_usb(cfg: Config, iso_path: str, target: str) -> None:
    """Extract a non-hybrid ISO to a USB drive with proper boot setup.

    Mounts or extracts the ISO contents, determines boot type (UEFI vs
    legacy), creates appropriate partition layout on the USB drive, and
    copies all files.
    """
    drive = _resolve_usb_target(cfg, target)
    if drive is None:
        return

    if not _usb_safety_checks(cfg, drive):
        return

    if not cfg.force:
        if not _cli_confirm_write(drive):
            cfg.log("Aborted.")
            return

    device = drive["path"]
    _unmount_device(cfg, device)
    _wipe_device(cfg, device)

    # Extract ISO to temp dir
    with tempfile.TemporaryDirectory() as extract_dir:
        cfg.log("  Extracting ISO contents...")
        if _which("xorriso"):
            _run(cfg, ["xorriso", "-osirrox", "on", "-indev", iso_path,
                       "-extract", "/", extract_dir],
                 verbose=cfg.verbose, check=False, as_root=True)
        else:
            # Fallback: mount and copy
            _run(cfg, ["bash", "-c",
                       f"MNTDIR=$(mktemp -d) && "
                       f"mount -o loop,ro '{iso_path}' $MNTDIR && "
                       f"cp -a $MNTDIR/* '{extract_dir}/' && "
                       f"umount $MNTDIR && rmdir $MNTDIR"],
                 verbose=cfg.verbose, as_root=True)

        # Determine boot type from extracted contents
        has_efi = Path(extract_dir, "EFI").is_dir()

        # Collect all extracted files
        files: dict[str, str] = {}
        extract_path = Path(extract_dir)
        for p in sorted(extract_path.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(extract_path))
                files[rel] = str(p)

        cfg.log(f"  Extracted {len(files)} files")

        if has_efi:
            # UEFI boot: create GPT + ESP on USB, copy files
            cfg.log("  UEFI boot detected, creating GPT layout...")
            cfg.gpt = True
            if not cfg.partitions:
                from mkimage import PartitionSpec
                cfg.partitions = [PartitionSpec("esp", "0", "BOOT")]
            _write_gpt_to_device(cfg, files, device)
        else:
            # No EFI: create simple FAT32, build temp image and dd
            cfg.log("  Creating FAT32 layout...")
            from mkimage import PartitionSpec
            if not cfg.partitions:
                cfg.partitions = [PartitionSpec("fat32", "0", "BOOT")]
            with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                build_img(cfg, files, tmp_path)
                img_size = os.path.getsize(tmp_path)
                img_resolved = _resolve(tmp_path)
                _write_usb_linux(cfg, tmp_path, img_size, img_resolved, drive)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    _run(cfg, ["sync"], check=False)
    cfg.log(f"  [OK] ISO extracted to {device}. You can safely remove it.")


def write_usb(
    cfg: Config,
    image_path: str,
    source_dir: str = "",
    includes: Optional[list[str]] = None,
    select_drive: Optional[Callable[..., Optional[dict[str, str]]]] = None,
    confirm_write: Optional[Callable[..., bool]] = None,
) -> None:
    """Write files to a USB drive with safety checks.

    On Windows: uses diskpart to format USB as FAT32, then copies files directly.
    On Linux: writes a pre-built .img file via dd.

    Args:
        cfg: Runtime configuration.
        image_path: Path to .img file (used on Linux for dd, or as fallback on Windows).
        source_dir: Source directory containing files to copy (Windows diskpart path).
        includes: Extra files/directories to include (Windows diskpart path).
        select_drive: Callback(drives_list) -> drive_dict or None. Defaults to CLI input().
        confirm_write: Callback(target_dict) -> bool. Defaults to CLI input().
    """
    if select_drive is None:
        select_drive = _cli_select_drive
    if confirm_write is None:
        confirm_write = _cli_confirm_write

    cfg.log("Scanning for removable drives...")
    drives = _list_removable_drives()

    if not drives:
        cfg.log("No removable USB drives found.")
        cfg.log("  - On Windows/WSL, USB passthrough may need usbipd")
        cfg.log(f"  - Drive must be removable and under {MAX_USB_SIZE_GB}GB")
        return

    cfg.log(f"Found {len(drives)} removable drive(s)")
    target = select_drive(drives)
    if target is None:
        cfg.log("Aborted.")
        return

    target_path = target["path"]
    target_size = int(target["size_bytes"])

    # Safety checks
    if "sda" in target["name"] and not _is_windows():
        mr = _run(cfg, ["lsblk", "-n", "-o", "MOUNTPOINT", target_path], check=False)
        mounts = mr.stdout.strip() if mr.returncode == 0 else ""
        if "/" in mounts or "/boot" in mounts or "/home" in mounts:
            cfg.log(f"Error: {target_path} has system partitions mounted. Refusing.")
            return

    if target_size > MAX_USB_SIZE_GB * (1024 ** 3):
        cfg.log(f"Error: {target_path} is larger than {MAX_USB_SIZE_GB}GB. Refusing.")
        return

    if not confirm_write(target):
        cfg.log("Aborted.")
        return

    if _is_windows():
        _write_usb_windows(cfg, source_dir, includes or [], target)
    else:
        if not os.path.isfile(image_path):
            cfg.log(f"Error: {image_path} not found")
            return
        img_resolved = _resolve(image_path)
        img_size = os.path.getsize(image_path)
        _write_usb_linux(cfg, image_path, img_size, img_resolved, target)


def _write_usb_windows(
    cfg: Config, source_dir: str, includes: list[str],
    target: dict[str, str],
) -> None:
    """Format USB as FAT32 via diskpart and copy files directly.

    Uses an elevated PowerShell subprocess for the diskpart + copy.
    No raw disk write -- just format and file copy.
    """
    target_path = target["path"]  # \\.\PhysicalDriveN
    disk_num = target_path.rsplit("PhysicalDrive", 1)[-1]
    label_trim = cfg.label[:11]

    # Determine filesystem from first partition spec
    fs_type = "FAT32"
    if cfg.partitions:
        fs_map = {"fat32": "FAT32", "ntfs": "NTFS", "exfat": "exFAT",
                  "esp": "FAT32"}
        fs_type = fs_map.get(cfg.partitions[0].fs_type, "FAT32")

    cfg.log(f"Formatting disk {disk_num} as {fs_type} and copying files...")

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
    verbose_str = "$true" if cfg.verbose else "$false"
    verify_str = "$true" if cfg.verify else "$false"
    gpt_str = "$true" if cfg.gpt else "$false"

    ps_script = f"""
# Force every Out-File below to UTF-8 so the Python side (which reads the
# progress file as utf-8-sig) gets clean text. Windows PowerShell's default
# Out-File encoding is UTF-16, which the reader would turn into mojibake.
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'
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
    ("select disk {disk_num}`r`nclean all" | diskpart 2>&1) | Out-Null
    Start-Sleep -Seconds 1

    # Step 3: convert (separate session, ignore failure if already correct type)
    "  diskpart: convert $partStyle..." | Out-File -Append '{prg_esc}'
    ("select disk {disk_num}`r`nconvert $($partStyle.ToLower())" | diskpart 2>&1) | Out-Null
    Start-Sleep -Seconds 1

    # Step 4: create + format (separate session)
    $fsType = "{fs_type.lower()}"
    if ($useGpt) {{
        $dpCreate = @"
select disk {disk_num}
create partition primary
select partition 1
format fs=$fsType quick label={label_trim}
"@
    }} else {{
        $dpCreate = @"
select disk {disk_num}
create partition primary
active
format fs=$fsType quick label={label_trim}
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
        "  Add-PartitionAccessPath ${{driveLetter}}:\\" | Out-File -Append '{prg_esc}'
        $part | Add-PartitionAccessPath -AccessPath "${{driveLetter}}:\\" -ErrorAction Stop
    }}
    Start-Sleep -Seconds 3

    $destRoot = "${{driveLetter}}:\\"
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
            $srcNorm = $src.TrimEnd('\\', '/')
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

    if (Test-Path "${{driveLetter}}:\\") {{
        $fileCount = (Get-ChildItem "${{driveLetter}}:\\" -Recurse -File -ErrorAction SilentlyContinue).Count
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
                $srcNorm = $src.TrimEnd('\\', '/')
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
        cfg.log("  Requesting Administrator access...")
        proc = subprocess.Popen([
            "powershell.exe", "-NoProfile", "-Command",
            f"Start-Process -FilePath 'powershell.exe' "
            f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','{script_file}' "
            f"-Verb RunAs -Wait"
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        _poll_progress(proc, progress_file, cfg.log)

    except Exception as exc:
        cfg.log(f"[ERROR] {exc}")
    finally:
        for f in (script_file, progress_file):
            try:
                os.unlink(f)
            except OSError:
                pass


def _poll_progress(proc: subprocess.Popen[bytes], progress_file: str,
                   log: Callable[..., None]) -> None:
    """Poll a progress file while a subprocess runs, logging new lines.

    Reads the final status line after the process exits and logs the result.
    """
    import time
    lines_read = 0
    while proc.poll() is None:
        time.sleep(0.3)
        if os.path.exists(progress_file):
            try:
                with open(progress_file, "r", encoding="utf-8-sig", errors="replace") as pf:
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


def _write_usb_linux(
    cfg: Config, image_path: str, img_size: int, img_resolved: str,
    target: dict[str, str],
) -> None:
    """Write image to USB on Linux using dd."""
    target_path = target["path"]

    # Unmount any mounted partitions
    cfg.log(f"Unmounting {target_path} partitions...")
    _run(cfg, ["umount", f"{target_path}*"], check=False)
    for i in range(1, 10):
        _run(cfg, ["umount", f"{target_path}{i}"], check=False)
        _run(cfg, ["umount", f"{target_path}p{i}"], check=False)

    cfg.log(f"Writing {image_path} ({img_size // (1024*1024)}MB) to {target_path}...")
    dd_cmd = ["dd", f"if={img_resolved}", f"of={target_path}", "bs=4M", "conv=fsync"]
    if cfg.verbose:
        dd_cmd.insert(-1, "status=progress")
    r = _run(cfg, dd_cmd, check=False, verbose=True)

    if r.returncode != 0:
        cfg.log("  Retrying as root...")
        r = _run(cfg, [
            "dd", f"if={img_resolved}", f"of={target_path}",
            "bs=4M", "conv=fsync",
        ], check=False, verbose=True, as_root=True)

    if r.returncode != 0:
        cfg.log(f"[ERROR] dd failed: {r.stderr.strip()}")
        cfg.log("  You may need to run with sudo.")
        return

    _run(cfg, ["sync"], check=False)
    cfg.log(f"[OK] Wrote {img_size // (1024*1024)}MB to {target_path}. "
        f"You can safely remove the USB drive.")


def _write_usb_from_dir(cfg: Config, files: dict[str, str],
                        target: str, source_dir: str = "",
                        includes: Optional[list[str]] = None) -> None:
    """Build and write to a USB device from collected files.

    source_dir/includes are the original source paths, needed by the Windows
    diskpart path (which robocopies the source tree directly). The GPT/Linux
    paths build from the collected `files` mapping instead.
    """
    drive = _resolve_usb_target(cfg, target)
    if drive is None:
        return

    if not _usb_safety_checks(cfg, drive):
        return

    if not cfg.force:
        if not _cli_confirm_write(drive):
            cfg.log("Aborted.")
            return

    device = drive["path"]
    _unmount_device(cfg, device)
    _wipe_device(cfg, device)

    if cfg.gpt:
        # GPT direct to device
        cfg.log(f"Writing GPT layout to {device}...")
        _write_gpt_to_device(cfg, files, device)
    elif _is_windows():
        _write_usb_windows(cfg, source_dir, includes or [], drive)
    else:
        # Build temp FAT32 image, then dd
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            cfg.log("Building FAT32 image...")
            build_img(cfg, files, tmp_path)
            img_size = os.path.getsize(tmp_path)
            img_resolved = _resolve(tmp_path)
            _write_usb_linux(cfg, tmp_path, img_size, img_resolved, drive)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _write_usb_from_image(cfg: Config, image_path: str,
                          target: str) -> None:
    """Write an existing image file to a USB device via dd.

    For non-hybrid ISOs, extracts contents to a properly formatted USB
    instead of raw dd (which would produce an unbootable drive).
    """
    if not os.path.isfile(image_path):
        cfg.log(f"Error: {image_path} not found")
        return

    # Non-hybrid ISOs need extraction, not raw dd
    if image_path.lower().endswith('.iso') and not _is_hybrid_iso(image_path):
        cfg.log("  Non-hybrid ISO detected, extracting to USB...")
        _extract_iso_to_usb(cfg, image_path, target)
        return

    drive = _resolve_usb_target(cfg, target)
    if drive is None:
        return

    if not _usb_safety_checks(cfg, drive):
        return

    if not cfg.force:
        if not _cli_confirm_write(drive):
            cfg.log("Aborted.")
            return

    _unmount_device(cfg, drive["path"])
    _wipe_device(cfg, drive["path"])
    img_resolved = _resolve(image_path)
    img_size = os.path.getsize(image_path)
    _write_usb_linux(cfg, image_path, img_size, img_resolved, drive)


def format_device(cfg: Config, device: str) -> None:
    """Format a USB device with partition table and filesystem (no files).

    Creates partition table (GPT/MBR/none) and formats partitions based
    on cfg.partitions. Requires root on Linux for sgdisk/mkfs operations.
    """
    from mkimage import PartitionSpec

    if _is_macos():
        _format_device_macos(cfg, device)
        return

    if _is_windows():
        _format_device_windows(cfg, device)
        return

    # Linux
    _wipe_device(cfg, device)
    partitions = cfg.partitions or [PartitionSpec("fat32", "0", cfg.label)]

    if cfg.gpt:
        _check_root(cfg, "GPT format")
        cfg.log(f"  Creating GPT partition table on {device}...")
        _run(cfg, ["sgdisk", "-Z", device], verbose=cfg.verbose, as_root=True)
        _run(cfg, ["sgdisk", "-o", device], verbose=cfg.verbose, as_root=True)

        for i, part in enumerate(partitions):
            pnum = i + 1
            is_esp = part.fs_type == "esp"
            fs_type = "fat32" if is_esp else part.fs_type
            sgdisk_type = "EF00" if is_esp else "0700"
            label = part.label[:11] if part.label else cfg.label[:11]
            size_mb = _interpret_size(part.size, 1, is_esp=is_esp)

            if size_mb == 0 or i == len(partitions) - 1:
                size_spec = "0:0"
            else:
                size_spec = f"+{size_mb}M"
            start = "2048" if pnum == 1 else "0"

            _run(cfg, ["sgdisk",
                       "-n", f"{pnum}:{start}:{size_spec}",
                       "-t", f"{pnum}:{sgdisk_type}",
                       "-c", f"{pnum}:{label}",
                       device], verbose=True, as_root=True)

        _run(cfg, ["partprobe", device], check=False, as_root=True)
        import time
        time.sleep(1)

        part_fmt = _partition_device_fmt(device)
        for i, part in enumerate(partitions):
            pnum = i + 1
            is_esp = part.fs_type == "esp"
            fs_type = "fat32" if is_esp else part.fs_type
            label = part.label[:11] if part.label else cfg.label[:11]
            part_dev = part_fmt.format(pnum)
            cs = part.cluster_size if hasattr(part, 'cluster_size') else 0
            cfg.log(f"  Formatting partition {pnum} ({label}) as {fs_type} on {part_dev}...")
            _format_partition(cfg, part_dev, fs_type, label, cs)

    elif cfg.mbr:
        _check_root(cfg, "MBR format")
        cfg.log(f"  Creating MBR partition table on {device}...")
        part = partitions[0]
        fs_type = part.fs_type if part.fs_type != "esp" else "fat32"
        label = part.label[:11] if part.label else cfg.label[:11]
        sfdisk_input = "label: dos\ntype=0c\n"
        _run(cfg, ["sfdisk", device], verbose=cfg.verbose, as_root=True,
             input=sfdisk_input)
        _run(cfg, ["partprobe", device], check=False, as_root=True)
        import time
        time.sleep(1)
        part_fmt = _partition_device_fmt(device)
        part_dev = part_fmt.format(1)
        cs = part.cluster_size if hasattr(part, 'cluster_size') else 0
        cfg.log(f"  Formatting {part_dev} as {fs_type}...")
        _format_partition(cfg, part_dev, fs_type, label, cs)

    else:
        # Raw: format the whole device as a single filesystem
        part = partitions[0]
        fs_type = part.fs_type if part.fs_type != "esp" else "fat32"
        label = part.label[:11] if part.label else cfg.label[:11]
        cs = part.cluster_size if hasattr(part, 'cluster_size') else 0
        cfg.log(f"  Formatting {device} as {fs_type}...")
        _format_partition(cfg, device, fs_type, label, cs)

    _run(cfg, ["sync"], check=False)
    cfg.log(f"  [OK] {device} formatted.")


def _format_device_macos(cfg: Config, device: str) -> None:
    """Format a USB device on macOS using diskutil."""
    from mkimage import PartitionSpec
    partitions = cfg.partitions or [PartitionSpec("fat32", "0", cfg.label)]
    part = partitions[0]
    fs_type = part.fs_type if part.fs_type != "esp" else "fat32"
    label = part.label or cfg.label

    fs_map = {"fat32": "FAT32", "exfat": "ExFAT", "ntfs": "NTFS"}
    diskutil_fs = fs_map.get(fs_type, "FAT32")

    if cfg.gpt:
        scheme = "GPTFormat"
    elif cfg.mbr:
        scheme = "MBRFormat"
    else:
        scheme = "MBRFormat"

    cfg.log(f"  Formatting {device} as {diskutil_fs} ({scheme})...")
    _run(cfg, ["diskutil", "eraseDisk", diskutil_fs, label, scheme, device],
         verbose=True, as_root=True)
    cfg.log(f"  [OK] {device} formatted.")


def _format_device_windows(cfg: Config, device: str) -> None:
    """Format a USB device on Windows via PowerShell/diskpart."""
    from mkimage import PartitionSpec
    partitions = cfg.partitions or [PartitionSpec("fat32", "0", cfg.label)]
    part = partitions[0]
    fs_type = part.fs_type if part.fs_type != "esp" else "fat32"
    label = part.label[:11] if part.label else cfg.label[:11]

    fs_map = {"fat32": "fat32", "exfat": "exfat", "ntfs": "ntfs"}
    diskpart_fs = fs_map.get(fs_type, "fat32")

    gpt_str = "$true" if cfg.gpt else "$false"
    # Extract disk number from device path (\\.\PhysicalDriveN)
    disk_num = device.rsplit("PhysicalDrive", 1)[-1] if "PhysicalDrive" in device else device

    cfg.log(f"  Formatting disk {disk_num} as {diskpart_fs}...")
    _write_usb_windows_format_only(cfg, disk_num, diskpart_fs, label, gpt_str)


def _write_usb_windows_format_only(
    cfg: Config, disk_num: str, fs: str, label: str, gpt_str: str,
) -> None:
    """Run a format-only diskpart script on Windows (no file copy)."""
    fd, progress_file = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    fd, script_file = tempfile.mkstemp(suffix=".ps1")
    os.close(fd)

    prg_esc = progress_file.replace("'", "''")
    ps_script = f"""
# UTF-8 progress file (default Out-File is UTF-16, which the reader would
# turn into mojibake -> "produced no output"). Matches _write_usb_windows.
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'
try {{
    $useGpt = {gpt_str}
    $partStyle = if ($useGpt) {{ "GPT" }} else {{ "MBR" }}
    "Formatting disk {disk_num} as {fs} ($partStyle)..." | Out-File -Append '{prg_esc}'
    Set-Disk -Number {disk_num} -IsOffline $true -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    ("select disk {disk_num}`r`nclean" | diskpart 2>&1) | Out-Null
    Start-Sleep -Seconds 1
    ("select disk {disk_num}`r`nconvert $($partStyle.ToLower())" | diskpart 2>&1) | Out-Null
    Start-Sleep -Seconds 1
    $dpCreate = @"
select disk {disk_num}
create partition primary
format fs={fs} quick label={label}
"@
    ($dpCreate | diskpart 2>&1) | Out-Null
    "automount disable" | diskpart 2>&1 | Out-Null
    Set-Disk -Number {disk_num} -IsOffline $false -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    "automount enable" | diskpart 2>&1 | Out-Null
    "OK" | Out-File -Append '{prg_esc}'
}} catch {{
    "ERROR: $_" | Out-File -Append '{prg_esc}'
}}
"""
    with open(script_file, "w") as f:
        f.write(ps_script)

    try:
        proc = subprocess.Popen([
            "powershell.exe", "-NoProfile", "-Command",
            f"Start-Process -FilePath 'powershell.exe' "
            f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','{script_file}' "
            f"-Verb RunAs -Wait"
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.wait()

        final_msg = ""
        if os.path.exists(progress_file):
            with open(progress_file, "r", encoding="utf-8", errors="replace") as pf:
                lines = pf.readlines()
            for line in lines:
                stripped = line.rstrip()
                if stripped:
                    cfg.log(f"  {stripped}")
            final_msg = lines[-1].strip() if lines else ""

        if final_msg.startswith("OK"):
            cfg.log(f"  [OK] Disk {disk_num} formatted.")
        elif final_msg.startswith("ERROR"):
            raise RuntimeError(final_msg)
        else:
            raise RuntimeError("Format subprocess produced no output.")
    finally:
        for path in (script_file, progress_file):
            try:
                os.unlink(path)
            except OSError:
                pass


def _partition_device_fmt(device: str) -> str:
    """Determine partition device naming: sdX1, sdXp1, or diskNs1 (macOS)."""
    if _is_macos():
        return f"{device}s{{}}"
    if Path(f"{device}1").exists() or Path(f"{device}1").is_block_device():
        return f"{device}{{}}"
    return f"{device}p{{}}"


def _write_gpt_to_device(cfg: Config, files: dict[str, str],
                          device: str) -> None:
    """Write GPT layout directly to a USB device (no intermediate image).

    Iterates cfg.partitions to create N partitions on the device.
    Requires root for sgdisk on a device and mount operations.
    """
    from mkimage import PartitionSpec

    _check_root(cfg, "GPT USB write")
    ensure_tools(cfg, "gpt")

    partitions = cfg.partitions if cfg.partitions else [PartitionSpec("esp")]

    # Calculate sizes and collect files for each partition
    part_info: list[dict[str, object]] = []
    for i, part in enumerate(partitions):
        if i == 0:
            pfiles = files
        elif part.source_dir:
            pfiles = collect_files(cfg, part.source_dir, [])
        else:
            pfiles = {}
        content_mb = _calculate_content_size(pfiles) if pfiles else 1
        is_esp = part.fs_type == "esp"
        size_mb = _interpret_size(part.size, content_mb, is_esp=is_esp)
        fs_type = "fat32" if part.fs_type == "esp" else part.fs_type
        sgdisk_type = "EF00" if part.fs_type == "esp" else "0700"
        part_info.append({
            "spec": part,
            "files": pfiles,
            "content_mb": content_mb,
            "size_mb": size_mb,
            "fs_type": fs_type,
            "sgdisk_type": sgdisk_type,
            "label": part.label[:11],
        })

    with tempfile.TemporaryDirectory() as staging:
        # Stage files for each partition
        staging_dirs: list[str] = []
        for i, info in enumerate(part_info):
            pdir = Path(staging) / f"part{i}"
            pdir.mkdir()
            pfiles = info["files"]
            if pfiles:
                _stage_files(pfiles, pdir)  # type: ignore[arg-type]
            staging_dirs.append(_resolve(str(pdir)))

        # Partition the device directly
        cfg.log(f"  Creating GPT partition table on {device}...")
        _run(cfg, ["sgdisk", "-Z", device], verbose=cfg.verbose, as_root=True)
        _run(cfg, ["sgdisk", "-o", device], verbose=cfg.verbose, as_root=True)

        for i, info in enumerate(part_info):
            pnum = i + 1
            size_mb = int(info["size_mb"])  # type: ignore[arg-type]
            label = str(info["label"])
            sgdisk_type = str(info["sgdisk_type"])

            if size_mb == 0:
                size_spec = "0:0"
            elif i == len(part_info) - 1:
                size_spec = "0:0"
            else:
                size_spec = f"+{size_mb}M"

            start = "2048" if pnum == 1 else "0"
            _run(cfg, ["sgdisk",
                       "-n", f"{pnum}:{start}:{size_spec}",
                       "-t", f"{pnum}:{sgdisk_type}",
                       "-c", f"{pnum}:{label}",
                       device], verbose=True, as_root=True)

        # Re-read partition table
        _run(cfg, ["partprobe", device], check=False, as_root=True)
        import time
        time.sleep(1)

        part_fmt = _partition_device_fmt(device)

        for i, info in enumerate(part_info):
            pnum = i + 1
            label = str(info["label"])
            fs_type = str(info["fs_type"])
            pfiles = info["files"]

            part_dev = part_fmt.format(pnum)
            cfg.log(f"  Formatting partition {pnum} ({label}) on {part_dev}...")
            spec = info["spec"]
            cs = spec.cluster_size if hasattr(spec, 'cluster_size') else 0  # type: ignore[union-attr]
            _format_partition(cfg, part_dev, fs_type, label, cs)

            if pfiles:
                cfg.log(f"  Copying {len(pfiles)} files to partition {pnum}...")  # type: ignore[arg-type]
                _populate_partition(cfg, staging_dirs[i], part_dev)

    _run(cfg, ["sync"], check=False)
    cfg.log(f"  [OK] USB drive {device} ready. You can safely remove it.")


def _get_device_size(cfg: Config, device: str) -> int:
    """Get the size of a block device in bytes.

    Uses blockdev --getsize64 on Linux, diskutil info on macOS.
    Returns 0 if the size cannot be determined.
    """
    if _is_macos():
        r = _run(cfg, ["diskutil", "info", device], check=False)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if "Disk Size:" in line or "Total Size:" in line:
                    # Extract byte count from parenthetical, e.g. "(15728640000 Bytes)"
                    import re
                    m = re.search(r"\((\d+)\s+[Bb]ytes", line)
                    if m:
                        return int(m.group(1))
        return 0

    r = _run(cfg, ["blockdev", "--getsize64", device],
             check=False, as_root=True)
    if r.returncode == 0 and r.stdout.strip():
        try:
            return int(r.stdout.strip())
        except ValueError:
            pass
    return 0


def _clone_usb_to_image(cfg: Config, source_device: str,
                         output_path: str) -> None:
    """Clone a USB drive to an image file (or compressed image).

    Reads the entire device via dd and writes to the output path.
    If the output path has a compression extension (.gz, .xz), the
    data is piped through the appropriate compressor.
    """
    from mkimage.compress import _is_compressed_path

    if not _verify_usb_bus(cfg, source_device):
        cfg.log(f"Error: {source_device} is not on the USB bus. Refusing.")
        return

    dev_size = _get_device_size(cfg, source_device)
    if dev_size > 0:
        cfg.log(f"  Source device size: {dev_size // (1024 * 1024)}MB")

    _unmount_device(cfg, source_device)

    compressed = _is_compressed_path(output_path)
    if compressed:
        ext = Path(output_path).suffix.lower()
        if ext == ".gz":
            compressor = "gzip -c"
        elif ext == ".xz":
            compressor = "xz -c"
        elif ext == ".zst":
            compressor = "zstd -c"
        else:
            compressor = "gzip -c"

        cfg.log(f"  Cloning {source_device} to {output_path} (compressed)...")
        pipe_cmd = f"dd if={source_device} bs=4M status=progress | {compressor} > '{output_path}'"
        r = _run(cfg, ["bash", "-c", pipe_cmd],
                 check=False, verbose=True, as_root=True)
    else:
        cfg.log(f"  Cloning {source_device} to {output_path}...")
        r = _run(cfg, ["dd", f"if={source_device}", f"of={output_path}",
                        "bs=4M", "status=progress"],
                 check=False, verbose=True, as_root=True)

    if r.returncode != 0:
        cfg.log(f"[ERROR] Clone failed: {r.stderr.strip()}")
        return

    _run(cfg, ["sync"], check=False)

    out_size = os.path.getsize(output_path) if os.path.isfile(output_path) else 0
    cfg.log(f"  Output file: {out_size // (1024 * 1024)}MB")

    if cfg.verify and not compressed:
        cfg.log("  Verifying clone (SHA256 checksums)...")
        import hashlib
        bs = 4 * 1024 * 1024

        src_hash = hashlib.sha256()
        with open(source_device, "rb") as f:
            while True:
                chunk = f.read(bs)
                if not chunk:
                    break
                src_hash.update(chunk)

        dst_hash = hashlib.sha256()
        with open(output_path, "rb") as f:
            while True:
                chunk = f.read(bs)
                if not chunk:
                    break
                dst_hash.update(chunk)

        if src_hash.hexdigest() == dst_hash.hexdigest():
            cfg.log(f"  [OK] Verification passed: {src_hash.hexdigest()[:16]}...")
        else:
            cfg.log(f"  [FAIL] Verification FAILED!")
            cfg.log(f"    Source: {src_hash.hexdigest()}")
            cfg.log(f"    Output: {dst_hash.hexdigest()}")
            return
    elif cfg.verify and compressed:
        cfg.log("  Note: verification skipped for compressed output (decompress first)")

    cfg.log(f"  [OK] Cloned {source_device} to {output_path}.")


def _clone_usb_to_usb(cfg: Config, source_device: str,
                       target_device: str) -> None:
    """Clone one USB drive to another via dd.

    Both source and target must be USB devices. The target must be
    at least as large as the source. All data on the target is destroyed.
    """
    # Resolve "usb" auto-detect for source
    if source_device.lower() == "usb":
        src_drive = _resolve_usb_target(cfg, source_device)
        if src_drive is None:
            return
        source_device = src_drive["path"]

    # Resolve "usb" auto-detect for target
    if target_device.lower() == "usb":
        tgt_drive = _resolve_usb_target(cfg, target_device)
        if tgt_drive is None:
            return
        target_device = tgt_drive["path"]

    if source_device == target_device:
        cfg.log("Error: source and target are the same device.")
        return

    # Safety checks on both devices
    if not _verify_usb_bus(cfg, source_device):
        cfg.log(f"Error: {source_device} is not on the USB bus. Refusing.")
        return
    if not _verify_usb_bus(cfg, target_device):
        cfg.log(f"Error: {target_device} is not on the USB bus. Refusing.")
        return

    src_size = _get_device_size(cfg, source_device)
    tgt_size = _get_device_size(cfg, target_device)

    if src_size > 0:
        cfg.log(f"  Source: {source_device} ({src_size // (1024 * 1024)}MB)")
    if tgt_size > 0:
        cfg.log(f"  Target: {target_device} ({tgt_size // (1024 * 1024)}MB)")

    if src_size > 0 and tgt_size > 0 and tgt_size < src_size:
        cfg.log(f"Error: target ({tgt_size // (1024 * 1024)}MB) is smaller "
                f"than source ({src_size // (1024 * 1024)}MB). Refusing.")
        return

    # Check target is not a system disk
    tgt_drive_info = _resolve_usb_target(cfg, target_device)
    if tgt_drive_info is not None and not _usb_safety_checks(cfg, tgt_drive_info):
        return

    # Confirm destructive write
    if not cfg.force:
        print(f"\n  WARNING: ALL DATA on {target_device} WILL BE DESTROYED.\n")
        try:
            confirm = input(f"  Type 'yes' to clone {source_device} -> {target_device}: ").strip()
        except (EOFError, KeyboardInterrupt):
            confirm = ""
        if confirm != "yes":
            cfg.log("Aborted.")
            return

    _unmount_device(cfg, source_device)
    _unmount_device(cfg, target_device)
    _wipe_device(cfg, target_device)

    cfg.log(f"  Cloning {source_device} to {target_device}...")
    r = _run(cfg, ["dd", f"if={source_device}", f"of={target_device}",
                    "bs=4M", "conv=fsync", "status=progress"],
             check=False, verbose=True, as_root=True)

    if r.returncode != 0:
        cfg.log(f"[ERROR] Clone failed: {r.stderr.strip()}")
        return

    _run(cfg, ["sync"], check=False)

    if cfg.verify:
        cfg.log("  Verifying clone (SHA256 checksums)...")
        read_size = src_size if src_size > 0 else tgt_size
        if read_size <= 0:
            cfg.log("  Warning: cannot determine device size, skipping verification")
        else:
            import hashlib
            bs = 4 * 1024 * 1024

            src_hash = hashlib.sha256()
            bytes_read = 0
            with open(source_device, "rb") as f:
                while bytes_read < read_size:
                    to_read = min(bs, read_size - bytes_read)
                    chunk = f.read(to_read)
                    if not chunk:
                        break
                    src_hash.update(chunk)
                    bytes_read += len(chunk)

            tgt_hash = hashlib.sha256()
            bytes_read = 0
            with open(target_device, "rb") as f:
                while bytes_read < read_size:
                    to_read = min(bs, read_size - bytes_read)
                    chunk = f.read(to_read)
                    if not chunk:
                        break
                    tgt_hash.update(chunk)
                    bytes_read += len(chunk)

            if src_hash.hexdigest() == tgt_hash.hexdigest():
                cfg.log(f"  [OK] Verification passed: {src_hash.hexdigest()[:16]}...")
            else:
                cfg.log(f"  [FAIL] Verification FAILED!")
                cfg.log(f"    Source: {src_hash.hexdigest()}")
                cfg.log(f"    Target: {tgt_hash.hexdigest()}")
                return

    cfg.log(f"  [OK] Cloned {source_device} to {target_device}. "
            f"You can safely remove the target drive.")
