# mkimage

Cross-platform tool for creating bootable disk images, ISOs, and
writing to USB drives. Works on Linux, macOS (native), and Windows
(via WSL bridge or native PowerShell). Not tied to any specific
project — takes a directory of files and produces bootable media.

## Design Doc (MANDATORY)

- ALWAYS read `docs/Design.md` before making changes
- This tool must remain GENERIC — no project-specific logic
- Cross-platform support is a core requirement, not optional

## Project Layout

```
mkimage/              Python package
  __init__.py         Config, PartitionSpec, public API exports
  __main__.py         CLI entry point (python -m mkimage)
  cli.py              Argument parsing and dispatch
  platform.py         _is_windows, _is_macos, _run, _which, _find_tool
  tools.py            Tool detection, auto-install, get_available_filesystems
  partition.py        Partition formatting, loop device management
  compress.py         gzip/zstd compression support
  verify.py           SHA256 write verification
  detect.py           Auto-detect source/target types
  native_dialog.py    Native OS file dialogs (subprocess-based)
  uefi_ntfs.py        UEFI:NTFS driver download-on-demand
  builders/           Image builders (img, iso, gpt, mbr)
  usb/                USB drive detection, safety, and write
  gui_dpg.py          Dear PyGui GUI (modern, GPU-rendered)
  gui_tk.py           Tkinter GUI (stdlib fallback)
mkimage.py            Backward-compat wrapper (imports mkimage package)
mkimage.pyz           Zipapp distribution (single-file, ~57KB)
mkimage.ps1           Native Windows (PowerShell + WinForms GUI)
mkimage.bat           Windows batch launcher for mkimage.ps1
build_pyz.py          Zipapp builder script
docs/Design.md        Architecture and full specification
tests/                Test suite (Linux, macOS, Windows via QEMU)
```

## Origin

Extracted from `the original project's scripts/mkimage.py` where it was
originally written as a cross-platform tool with WSL bridge for
Windows. Being expanded with GPT partitioning, multi-partition
layouts, and USB safety features from `a deployment script`.

## Primary Consumers

- the consuming build project — build.sh calls mkimage.py via
  `$MKIMAGE_DIR` to create the final multi-tool UEFI boot image
- the original project — standalone image creation
- a deployment project — deploy.sh image/usb targets (planned)

## Usage

```bash
# Simple FAT32 image from a directory
mkimage.py --source <dir> --target output.img

# GPT with EFI System Partition
mkimage.py --source <dir> --target output.img --gpt

# GPT with ESP + data partition
mkimage.py --source <dir> --target output.img --gpt \
    --partition esp::BOOT --partition fat32:0:DATA:./data/

# ISO image
mkimage.py --source <dir> --target output.iso

# Write to USB (auto-detect drive)
mkimage.py --source <dir> --target usb

# Write to specific USB device
mkimage.py --source <dir> --target /dev/sdb

# Write existing image to USB
mkimage.py --source output.img --target usb

# Verify after build (SHA256 comparison)
mkimage.py --source <dir> --target output.img --verify

# Hybrid ISO (dd-writable to USB)
mkimage.py --source <dir> --target output.iso --iso-hybrid

# UDF bridge ISO (ISO 9660 + UDF, supports files >4GB)
mkimage.py --source <dir> --target output.iso --udf-bridge

# Compressed output
mkimage.py --source <dir> --target output.img.gz

# Custom partition (type:size:label)
mkimage.py --source <dir> --target output.img --partition fat32:+64M:TOOLS

# exFAT or NTFS partition
mkimage.py --source <dir> --target output.img --partition exfat::BIGFILES

# NTFS partition (with UEFI:NTFS driver auto-downloaded for boot)
mkimage.py --source <dir> --target output.img --gpt \
    --partition esp::BOOT --partition ntfs:0:DATA

# UDF partition
mkimage.py --source <dir> --target output.img --gpt --partition udf:0:MEDIA

# Modify existing image (add/remove files)
mkimage.py --modify output.img --add newfile.txt --remove old.txt

# Write ISO to bootable USB (auto-detects hybrid vs extraction)
mkimage.py --source ubuntu.iso --target usb

# Persistent Linux USB (adds ext4 casper-rw partition)
mkimage.py --source ubuntu.iso --target usb --persistent 4G

# Custom cluster size
mkimage.py --source <dir> --target output.img --cluster-size 32768

# Check USB drive for bad blocks (destructive)
mkimage.py --check-usb /dev/sdb

# Clone USB to image
mkimage.py --source /dev/sdb --target backup.img

# Clone USB to compressed image
mkimage.py --source /dev/sdb --target backup.img.gz

# Wipe all partition signatures from a device
mkimage.py --wipe /dev/sdb

# List removable drives
mkimage.py --list-drives

# Check tool availability
mkimage.py --check

# Backward-compatible syntax still works
mkimage.py <dir> -o output.img
```

## Coding Style

- Python: typed, passes Pylance strict mode, `from __future__ import annotations`
- No external packages — stdlib only (Python 3.7+)
- Cross-platform: use `platform.system()` checks, not hardcoded paths
- Windows: calls mkimage.ps1 natively for all operations (no WSL)
- Linux/macOS: uses native shell commands via `_run()`
- mkimage.ps1 is independent — shares no code with mkimage.py

## Key Constraints

- Must remain generic — no UEFI-specific, project-specific, or
  organization-specific logic
- CLI is the primary interface; GUI is optional
- Linux/macOS operations use native tools (dd, mkfs.vfat, sgdisk, mount)
- Windows operations use native PowerShell (mkimage.ps1) — no WSL needed
- USB write requires safety checks on all platforms
- No external Python packages — stdlib only
- GUI: Dear PyGui primary (auto-installed), Tkinter fallback (stdlib)
- File dialogs: native OS dialogs via subprocess (AppleScript on macOS,
  tkinter.filedialog on Windows/Linux, zenity/kdialog fallback)
- Supported filesystems: FAT32, exFAT, NTFS, ext4, UDF
  (availability varies by platform — use --check to verify)
- UEFI:NTFS: downloaded on demand from GitHub (GPL-2.0, not shipped)
