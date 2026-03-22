# mkimage Design Document

## Overview

mkimage is a cross-platform command-line tool for creating bootable
disk images, ISO images, and writing directly to USB drives. It takes
a directory of files and produces bootable media in various formats.

Works natively on Linux and macOS, and on Windows via WSL bridge.
A native Windows alternative (mkimage.ps1) is provided for
environments without WSL.

**Goal:** Single tool that replaces duplicated image-creation code
across multiple projects, running on any developer workstation.

## Current State

mkimage.py (1438 lines) was originally written for `uefi-ipmitool`
as a cross-platform tool. It currently supports:

- FAT32 image creation (dd + mkfs.vfat + mount + rsync)
- ISO image creation (xorriso or genisoimage)
- USB write with drive detection
- Windows support via WSL bridge (path conversion, `wsl -u root`)
- Tkinter GUI
- Auto-detection of missing tools with install hints

What it does NOT currently support (to be added):

- GPT partition tables
- Multi-partition layouts (ESP + data)
- USB safety checks (bus verification, size limits, confirmation)
- Auto-sizing based on content
- macOS support (untested)

## Architecture

```
mkimage.py (cross-platform, Python 3.7+, stdlib only)
  ├── CLI (argparse) — primary interface
  ├── GUI (tkinter) — optional interactive mode
  ├── Platform layer
  │   ├── Linux/macOS: native shell commands
  │   └── Windows: WSL bridge (_run() routes through wsl)
  ├── Image builders
  │   ├── FAT32 simple (dd + mkfs.vfat + mount + rsync)
  │   ├── GPT + ESP (sgdisk + mkfs.vfat + losetup + mount)  [NEW]
  │   ├── GPT + ESP + data (sgdisk + dual mkfs.vfat)         [NEW]
  │   └── ISO (xorriso or genisoimage)
  ├── USB writer
  │   ├── Linux: safety checks + dd
  │   └── Windows: drive detection + diskpart
  └── Utilities
      ├── Tool detection and install hints
      ├── Content size calculation
      └── Drive enumeration

mkimage.ps1 (Windows-native, PowerShell, independent)
  ├── WinForms GUI
  ├── VHD mount for image creation
  ├── diskpart for USB formatting
  └── IMAPI2 COM fallback for ISO

mkimage.bat (launcher for mkimage.ps1)
```

mkimage.py and mkimage.ps1 are independent implementations — they
share no code. mkimage.py is the primary tool; mkimage.ps1 exists
for Windows environments without WSL.

## CLI Interface

### Image creation

```
mkimage.py <dir> -o output.img                # Simple FAT32 image
mkimage.py <dir> -o output.img --label TOOLS  # Custom volume label
mkimage.py <dir> -o output.img --size 128     # Fixed size (MB)
mkimage.py <dir> -o output.img --extra 64     # Content + 64MB free
mkimage.py <dir> -o output.iso                # ISO image
mkimage.py <dir> -o output.iso --label TOOLS  # ISO with label
```

### GPT partition layouts [NEW]

```
mkimage.py <dir> -o output.img --gpt                    # GPT + single ESP
mkimage.py <dir> -o output.img --gpt --data-dir ./data/ # GPT + ESP + data partition
mkimage.py <dir> -o output.img --gpt --data-size 4G     # Fixed data partition size
mkimage.py <dir> -o output.img --gpt --esp-label ESP --data-label DATA
```

### USB write

```
mkimage.py <dir> --write-usb /dev/sdb                # FAT32 direct to USB
mkimage.py <dir> --write-usb /dev/sdb --gpt          # GPT layout to USB
mkimage.py --write-usb /dev/sdb --from-image out.img # Write existing image
mkimage.py --list-drives                              # List removable drives
```

### Additional options

```
mkimage.py --include extra/file.txt      # Extra files (repeatable)
mkimage.py --check                       # Verify tool availability
mkimage.py --gui                         # Launch Tkinter GUI
mkimage.py -v                            # Verbose output
mkimage.py --force                       # Skip USB confirmation
```

## Output Formats

### Simple FAT32 image (current)

Single FAT32 filesystem in a raw image file. No partition table.
Suitable for virtual floppy, small boot media, iDRAC virtual media.

```
[FAT32 filesystem]
  ├── EFI/BOOT/BOOTX64.EFI
  ├── tool.efi
  └── startup.nsh
```

Implementation: `dd` → `mkfs.vfat` → loop mount → `rsync` → `umount`

### GPT + ESP [NEW]

GPT partition table with a single EFI System Partition. Standard
layout for UEFI boot USB drives.

```
[GPT]
  └── Partition 1: ESP (type EF00, FAT32)
        ├── EFI/BOOT/BOOTX64.EFI
        ├── tool.efi
        └── startup.nsh
```

Implementation: `dd` (sparse) → `sgdisk` → `losetup` → `partprobe`
→ `mkfs.vfat` on partition → mount → `rsync` → `umount` → `losetup -d`

### GPT + ESP + Data [NEW]

GPT with ESP and a secondary FAT32 data partition. Used by SoftBMC
for config and documentation storage.

```
[GPT]
  ├── Partition 1: ESP (type EF00, FAT32)
  │     ├── EFI/BOOT/BOOTX64.EFI
  │     └── ...
  └── Partition 2: Data (type 0700, FAT32)
        ├── README.md
        └── config/
```

Implementation: same as GPT+ESP but with two `sgdisk -n` calls and
two `mkfs.vfat` + mount + rsync passes.

### ISO image (current)

ISO 9660 with Joliet extensions. Created via `xorriso` or
`genisoimage`.

## USB Write

### Safety checks [NEW — from softbmc deploy.sh]

Before writing to USB:

1. **Bus verification** — `udevadm info --query=property` confirms
   device is on USB bus. Rejects SATA, NVMe, etc.
2. **Size limit** — Rejects devices >= 300GB (configurable). Prevents
   accidental writes to system disks.
3. **Confirmation prompt** — Shows device name, size, and requires
   explicit "yes". Bypassed with `--force`.
4. **Unmount** — All mounted partitions on the device are unmounted
   before write.

On Windows, drive enumeration uses PowerShell `Get-Disk` /
`Get-Partition` via WSL bridge.

### Write methods

- **From directory:** creates image in temp file, then `dd` to device
- **From existing image:** direct `dd` to device
- **GPT layout:** `sgdisk` + `mkfs.vfat` directly on device (no
  intermediate image)

## Auto-sizing [NEW — from softbmc deploy.sh]

When `--size` is not specified, mkimage calculates the optimal image
size:

```
content_size = sum of all input files
esp_size = max(content_size * 1.3 + 10MB, 64MB)    # 30% margin, 64MB minimum
data_size = --data-size or remaining space
total = esp_size + data_size + 2MB (GPT overhead)
```

FAT32 requires 65525+ clusters. For very small content, the minimum
image size is enforced automatically.

## Platform Layer

### _run() function

All shell commands go through `_run()` which handles platform
differences:

```python
def _run(cmd, as_root=False):
    if _is_windows():
        # Route through WSL
        shell_cmd = cmd if isinstance(cmd, str) else ' '.join(cmd)
        if as_root:
            actual = ["wsl", "-u", "root", "bash", "-c", shell_cmd]
        else:
            actual = ["wsl", "bash", "-c", shell_cmd]
    else:
        # Native execution
        if as_root:
            actual = ["sudo", "bash", "-c", cmd]
        else:
            actual = ["bash", "-c", cmd]
    subprocess.run(actual, check=True)
```

### Path conversion

On Windows, paths are converted to WSL mount paths:
`C:\Users\mike\staging` → `/mnt/c/Users/mike/staging`

### Tool detection

`--check` verifies all required tools are available and reports
missing ones with platform-appropriate install commands:

```
$ mkimage.py --check
Environment: native
FAT32 (.img): OK
ISO   (.iso): OK
GPT   (.gpt): MISSING: gdisk
USB   write:  OK

Install: sudo dnf install gdisk
```

## Dependencies

### Required (all formats)

| Tool | Package (Fedora/RHEL) | Package (Debian/Ubuntu) | Purpose |
|------|----------------------|------------------------|---------|
| mkfs.vfat | dosfstools | dosfstools | FAT32 formatting |
| rsync | rsync | rsync | File copying to image |

### Optional (per feature)

| Tool | Package | Purpose |
|------|---------|---------|
| sgdisk | gdisk | GPT partition tables |
| xorriso | xorriso | ISO creation (preferred) |
| genisoimage | genisoimage | ISO creation (fallback) |
| udevadm | systemd | USB bus verification |
| losetup | util-linux | Loop device for image mount |

On macOS, install via Homebrew: `brew install dosfstools gdisk xorriso`

## Implementation Plan

### Phase 1: Extract and reorganize

- Copy mkimage.py, mkimage.ps1, mkimage.bat from uefi-ipmitool
- Add CLAUDE.md, docs/Design.md, .gitignore
- Verify existing functionality works standalone
- No code changes — just extraction

### Phase 2: Add GPT support

- Add `--gpt` flag to CLI
- Implement `build_gpt_img()` function:
  - `dd` sparse image
  - `sgdisk` for GPT + ESP partition
  - `losetup` + `partprobe` for partition access
  - `mkfs.vfat` + mount + rsync + umount
  - `losetup -d` cleanup
- Add `--data-dir` and `--data-size` for ESP + data layout
- Implement `build_gpt_data_img()` with two partitions

### Phase 3: USB safety and auto-sizing

- Add USB safety checks (udevadm bus verification, size limit,
  confirmation prompt, `--force` flag)
- Implement auto-sizing: content measurement, margin calculation,
  FAT32 minimum enforcement
- Add `--list-drives` command
- Add `--write-usb` with GPT support (direct partition on device)

### Phase 4: Polish

- Verify macOS compatibility
- Update Tkinter GUI to support new options (GPT, data partition)
- Update `--check` to report per-feature tool availability
- Update mkimage.ps1 if needed for feature parity

### Phase 5: Integration

- Update uefi-bootkit config.sh `MKIMAGE_DIR` default to point here
- Replace inline image creation in uefi-ipmitool/scripts/build.sh
- Replace image/usb targets in softbmc/scripts/deploy.sh
- Replace QEMU disk image creation in project qemu.sh scripts

## Implementation Status

### Phase 1: Extract and reorganize — DONE

| File | Status | Description |
|------|--------|-------------|
| `mkimage.py` | Done | Copied from uefi-ipmitool/scripts/ |
| `mkimage.ps1` | Done | Copied from uefi-ipmitool/scripts/ |
| `mkimage.bat` | Done | Copied from uefi-ipmitool/scripts/ |
| `CLAUDE.md` | Done | Project instructions |
| `docs/Design.md` | Done | This document |
| `.gitignore` | Done | Build artifacts |

### Phase 2: GPT support — TODO
### Phase 3: USB safety and auto-sizing — TODO
### Phase 4: Polish — TODO
### Phase 5: Integration — TODO
