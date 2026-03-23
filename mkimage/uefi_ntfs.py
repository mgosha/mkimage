"""UEFI:NTFS driver download and management.

Downloads the UEFI:NTFS bootloader and NTFS driver from GitHub releases
on demand, caching them locally. This enables booting from NTFS partitions
on UEFI systems.

The UEFI:NTFS project (github.com/pbatard/uefi-ntfs) is GPL-2.0 licensed.
Binaries are not shipped with mkimage — they are downloaded at runtime.
"""
from __future__ import annotations

import hashlib
import os
import platform
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mkimage import Config

# GitHub release URLs for UEFI:NTFS components
_UEFI_NTFS_RELEASE = "https://github.com/pbatard/uefi-ntfs/releases/download/v2.7"
_NTFS_3G_RELEASE = "https://github.com/pbatard/ntfs-3g/releases/download/v1.7"

# Architecture-specific filenames
_BOOTLOADERS = {
    "x64": "uefi-ntfs_x64.efi",
    "ia32": "uefi-ntfs_ia32.efi",
    "aa64": "uefi-ntfs_aa64.efi",
}

_NTFS_DRIVERS = {
    "x64": "ntfs_x64.efi",
    "ia32": "ntfs_ia32.efi",
    "aa64": "ntfs_aa64.efi",
}

_CACHE_DIR_NAME = "mkimage"
_UEFI_NTFS_SUBDIR = "uefi-ntfs"


def _cache_dir() -> Path:
    """Return the cache directory for UEFI:NTFS files."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        base = Path(xdg)
    elif platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif platform.system() == "Darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path.home() / ".cache"
    return base / _CACHE_DIR_NAME / _UEFI_NTFS_SUBDIR


def _download_file(url: str, dest: Path, cfg: Config) -> bool:
    """Download a file from URL to dest. Returns True on success."""
    try:
        cfg.log(f"  Downloading {url}...")
        dest.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, str(dest))
        return True
    except Exception as e:
        cfg.log(f"  Download failed: {e}")
        return False


def ensure_uefi_ntfs_files(cfg: Config) -> Path | None:
    """Download or locate UEFI:NTFS bootloader and driver files.

    Returns path to the cache directory containing the EFI files,
    or None if download failed. The directory contains:
      EFI/Boot/bootx64.efi   (UEFI:NTFS bootloader)
      EFI/Boot/ntfs_x64.efi  (NTFS filesystem driver)
      (and ia32/aa64 variants)
    """
    cache = _cache_dir()
    efi_boot = cache / "EFI" / "Boot"

    # Check if already cached
    marker = cache / ".downloaded"
    if marker.exists():
        return cache

    cfg.log("  UEFI:NTFS driver not cached. Downloading (GPL-2.0 licensed)...")

    efi_boot.mkdir(parents=True, exist_ok=True)
    ok = True

    # Download bootloaders
    for arch, filename in _BOOTLOADERS.items():
        # Map to standard UEFI boot filenames
        if arch == "x64":
            dest_name = "bootx64.efi"
        elif arch == "ia32":
            dest_name = "bootia32.efi"
        elif arch == "aa64":
            dest_name = "bootaa64.efi"
        else:
            continue
        url = f"{_UEFI_NTFS_RELEASE}/{filename}"
        if not _download_file(url, efi_boot / dest_name, cfg):
            ok = False

    # Download NTFS drivers (placed alongside bootloaders)
    for arch, filename in _NTFS_DRIVERS.items():
        url = f"{_NTFS_3G_RELEASE}/{filename}"
        if not _download_file(url, efi_boot / filename, cfg):
            ok = False

    if ok:
        marker.touch()
        cfg.log("  UEFI:NTFS files cached successfully.")
        return cache

    cfg.log("  WARNING: Some UEFI:NTFS files failed to download.")
    cfg.log("  NTFS boot may not work. Check your internet connection.")
    return cache if efi_boot.exists() else None
