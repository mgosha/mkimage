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
mkimage.py            Cross-platform image builder (Python 3.7+, no deps)
mkimage.ps1           Native Windows alternative (PowerShell + WinForms GUI)
mkimage.bat           Windows batch launcher for mkimage.ps1
docs/Design.md        Architecture and full specification
```

## Origin

Extracted from `uefi-ipmitool/scripts/mkimage.py` where it was
originally written as a cross-platform tool with WSL bridge for
Windows. Being expanded with GPT partitioning, multi-partition
layouts, and USB safety features from `softbmc/scripts/deploy.sh`.

## Primary Consumers

- `~/projects/aximcode/uefi-bootkit` — build.sh calls mkimage.py via
  `$MKIMAGE_DIR` to create the final multi-tool UEFI boot image
- `~/projects/aximcode/uefi-ipmitool` — standalone image creation
- `~/projects/aximcode/softbmc` — deploy.sh image/usb targets (planned)

## Usage

```bash
# Simple FAT32 image from a directory
mkimage.py --source <dir> --target output.img

# GPT with EFI System Partition
mkimage.py --source <dir> --target output.img --gpt

# GPT with ESP + data partition
mkimage.py --source <dir> --target output.img --gpt --data-dir ./data/

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

# Compressed output
mkimage.py --source <dir> --target output.img.gz

# exFAT or NTFS filesystem
mkimage.py --source <dir> --target output.img --fs exfat

# Modify existing image (add/remove files)
mkimage.py --modify output.img --add newfile.txt --remove old.txt

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
- WSL bridge: all shell commands go through `_run()` which routes
  through WSL on Windows
- mkimage.ps1 is independent — shares no code with mkimage.py

## Key Constraints

- Must remain generic — no UEFI-specific, project-specific, or
  organization-specific logic
- CLI is the primary interface; GUI is optional
- Linux/macOS operations use native tools (dd, mkfs.vfat, sgdisk, mount)
- Windows operations route through WSL or use native mkimage.ps1
- USB write requires safety checks on all platforms
- No external Python packages — stdlib only
