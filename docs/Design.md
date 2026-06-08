# mkimage Design Document

## Overview

mkimage is a cross-platform command-line tool for creating bootable
disk images, ISO images, and writing directly to USB drives. It takes
a directory of files and produces bootable media in various formats.

Works natively on Linux, macOS, and Windows. On Windows, all
operations use native PowerShell (mkimage.ps1) — no WSL needed.

**Goal:** Single tool that replaces duplicated image-creation code
across multiple projects, running on any developer workstation.

## Current State

mkimage is a Python package (~3200 lines) distributed as a 57KB zipapp.

**Supported features:**
- Image creation: FAT32, GPT (ESP + N partitions), MBR, ISO 9660
- Filesystems: FAT32, exFAT, NTFS, ext4, UDF (platform-dependent)
- ISO: standard, hybrid (dd-writable to USB), UDF bridge (>4GB files)
- USB: auto-detect drives, write images, write from directory, clone, wipe
- USB safety: bus verification, size limits, root partition protection
- Compression: .img.gz, .img.xz output; .zst if zstd installed
- Verification: SHA256 after build or USB write
- Bad block detection: --check-usb destructive write/read test
- Persistent storage: --persistent adds ext4 casper-rw for live USBs
- UEFI:NTFS: auto-download GPL-2.0 driver for NTFS boot partitions
- fat32format (Ridgecrop, GPL): auto-download for whole-disk FAT32 >32GB on
  Windows; 32GB-cap fallback. Optional third-party tool licensing is recorded
  in docs/THIRD_PARTY.md (both download-on-demand, not bundled in the public
  build; bundling permitted for an open-source app if source+GPL text ship).
- GUIs: native PowerShell WinForms GUI is the default on Windows; Dear
  PyGui (modern, auto-installed) + Tkinter fallback elsewhere. Both share
  the same tabbed layout/widget names; --native-gui / --python-gui override.
  See docs/Native-GUI-Parity-Plan.md.
- Native file dialogs: AppleScript (macOS), tkinter (Win/Linux), zenity/kdialog
- Windows: all operations via native PowerShell (mkimage.ps1), no WSL
- macOS: native tools (hdiutil, diskutil, newfs_msdos, newfs_exfat, newfs_udf)
- Tool auto-detection with install hints per platform

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

mkimage.py and mkimage.ps1 are independent implementations for image
and ISO building — they share no build code. The one exception is the
Windows USB write/format path: `mkimage/usb/write.py` DELEGATES to
`mkimage.ps1 -Action WriteUsb|Format` (via `_run_ps1_windows`), so there
is a single Windows USB engine (diskpart + robocopy + fat32format) rather
than two parallel diskpart scripts. mkimage.py is the primary tool;
mkimage.ps1 is the Windows engine and standalone GUI/CLI.

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

### Safety checks [NEW]

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

## Auto-sizing [NEW]

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
`C:\Users\<user>\staging` → `/mnt/c/Users/<user>/staging`

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

- Copy mkimage.py, mkimage.ps1, mkimage.bat from the original project
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

- Update the consuming project's config `MKIMAGE_DIR` default to point here
- Replace inline image creation in the original project's build script
- Replace image/usb targets in a deployment script
- Replace QEMU disk image creation in project qemu.sh scripts

## Implementation Status

### Phase 1: Extract and reorganize — DONE

| File | Status | Description |
|------|--------|-------------|
| `mkimage.py` | Done | Copied from the original project's scripts/ |
| `mkimage.ps1` | Done | Copied from the original project's scripts/ |
| `mkimage.bat` | Done | Copied from the original project's scripts/ |
| `CLAUDE.md` | Done | Project instructions |
| `docs/Design.md` | Done | This document |
| `.gitignore` | Done | Build artifacts |

### Phase 2: GPT support — DONE

| Feature | Status | Description |
|---------|--------|-------------|
| `--gpt` flag | Done | CLI and GUI support |
| `build_gpt_img()` | Done | GPT + single ESP (sgdisk + losetup + mount) |
| `build_gpt_data_img()` | Done | GPT + ESP + data partition |
| `--data-dir` | Done | Secondary data partition from directory |
| `--data-size` | Done | Fixed data partition size (e.g. 512M, 4G) |
| `--esp-label` / `--data-label` | Done | Custom partition labels |
| Auto-sizing | Done | ESP: max(content*1.3+10, 64)MB, 2MB GPT overhead |
| `check_tools_gpt()` | Done | sgdisk + losetup availability check |
| GUI GPT panel | Done | Toggleable panel with data dir, size, labels |
| Tests | Done | Structure tests (no root) + integration tests (root) |

### Phase 3: USB safety and CLI redesign — DONE

| Feature | Status | Description |
|---------|--------|-------------|
| `--source` / `--target` | Done | Auto-detecting CLI (dir/image → file/device/usb) |
| Backward compat | Done | Positional source_dir and -o still work |
| `--list-drives` | Done | List removable USB drives |
| `--force` | Done | Skip USB confirmation (safety checks still run) |
| udevadm bus verification | Done | Rejects non-USB devices (SATA, NVMe) |
| MAX_USB_SIZE_GB | Done | Updated to 300GB (was 256GB) |
| Improved unmount | Done | Uses findmnt for reliable partition detection |
| USB write from dir | Done | Build temp image + dd, or GPT direct to device |
| USB write from image | Done | dd existing .img to USB |
| GPT direct to device | Done | sgdisk + mkfs.vfat directly on USB device |
| USB auto-detect | Done | `--target usb` auto-selects single drive |

### Phase 4: macOS support and polish — DONE

| Feature | Status | Description |
|---------|--------|-------------|
| `_is_macos()` | Done | Platform detection for Darwin |
| Homebrew support | Done | `brew` in package manager detection + tool mappings |
| `_find_tool()` | Done | Resolves Homebrew sbin paths not in PATH |
| hdiutil attach/detach | Done | Replaces losetup on macOS for GPT images |
| diskutil list external | Done | Replaces lsblk for USB drive enumeration |
| diskutil info Protocol | Done | Replaces udevadm for USB bus verification |
| diskutil unmountDisk | Done | Replaces findmnt for partition unmounting |
| macOS partition naming | Done | /dev/diskNsM instead of /dev/loopNpM |
| macOS tests | Done | 7 SSH-based tests (--check, img, iso, gpt, drives) |

### New features

| Feature | Status | Description |
|---------|--------|-------------|
| Write verification | Done | `--verify` SHA256 comparison after build via mcopy |
| ISO hybrid | Done | `--iso-hybrid` creates dd-writable ISO with EFI boot image |
| Image compression | Done | `.img.gz`, `.img.xz` output; `.zst` if zstd installed |
| Image modify | Done | `--modify image.img --add file --remove file` via mtools |
| NTFS/exFAT | Done | Per-partition filesystem via `--partition exfat::LABEL` |
| `_format_partition()` | Done | DRY helper replaces inline mkfs calls everywhere |
| Unified `--partition` | Done | `TYPE:SIZE:LABEL[:DIR]` replaces 6 scattered flags |
| `PartitionSpec` | Done | Dataclass for per-partition config, N-partition support |
| N-partition GPT | Done | `build_gpt_img` handles 1..N partitions via spec list |
| ISO to USB extraction | Done | Non-hybrid ISOs extracted to bootable USB (EFI auto-detect) |
| Bad block detection | Done | `--check-usb` destructive write/read test |
| Persistent partition | Done | `--persistent 4G` adds ext4 casper-rw for Linux live USBs |
| Cluster size | Done | `--cluster-size` or per-partition in PartitionSpec |
| Disk wipe | Done | `--wipe` removes all partition signatures (MBR, GPT, FS) |
| Windows native | Done | All operations via mkimage.ps1, no WSL needed |
| macOS support | Done | hdiutil/diskutil for GPT, USB, drive detection |
| Dear PyGui GUI | Done | Modern GPU-accelerated GUI with tabbed layout |
| Multi-boot | TODO | GRUB2 menu + multi-ISO USB (deferred — high complexity) |
| GRUB bootloader gen | TODO | Generate GRUB config for persistent live USBs (mkusb parity) |
| Windows ISO WIM split | TODO | Split >4GB install.wim for FAT32 USBs (mkusb-tow parity) |
| USB clone | Done | Clone USB drive to image or another USB (dd-based) |
| UDF filesystem | Done | UDF partition type + UDF bridge ISO (ISO 9660 + UDF) |
| UDF bridge ISO | Done | `--udf-bridge` creates dual ISO 9660 + UDF (via genisoimage -udf) |
| UEFI:NTFS | Done | GPL-2.0 driver downloaded on-demand for NTFS boot partitions |
| Native file dialogs | Done | AppleScript (macOS), tkinter subprocess, zenity/kdialog |
| ext4 filesystem | Done | For persistent partitions (Linux only) |
| Filesystem detection | Done | `get_available_filesystems()`, GUIs grey out unavailable types |
| PS1 multi-filesystem | Done | mkimage.ps1 supports FAT32/NTFS/exFAT via diskpart |

### Phase 5: Integration — TODO
