"""Source and target type detection."""
from __future__ import annotations

from pathlib import Path

from mkimage.compress import _strip_compression_ext


def _detect_source_type(source: str) -> str:
    """Detect source type: 'directory', 'image', 'device', or 'usb-auto'.

    Raises ValueError for unrecognized source.
    """
    if source.lower() == "usb":
        return "usb-auto"
    if source.startswith("/dev/") or source.startswith("\\\\.\\"):
        return "device"
    p = Path(source)
    if p.is_dir():
        return "directory"
    if p.is_file() and p.suffix.lower() in (".img", ".iso"):
        return "image"
    if p.is_file():
        return "image"  # treat any file as image for dd
    raise ValueError(f"Source '{source}' is not a directory, file, or device")


def _detect_target_type(target: str) -> str:
    """Detect target type: 'img', 'iso', 'device', or 'usb-auto'.

    Handles compressed extensions: .img.gz, .iso.xz, etc.
    Raises ValueError for unrecognized target.
    """
    if target.lower() == "usb":
        return "usb-auto"
    if target.startswith("/dev/") or target.startswith("\\\\.\\"):
        return "device"
    # Strip compression extension for type detection
    base = _strip_compression_ext(target)
    ext = Path(base).suffix.lower()
    if ext == ".iso":
        return "iso"
    if ext == ".img":
        return "img"
    raise ValueError(
        f"Cannot determine target type for '{target}'. "
        f"Use .img, .iso, .img.gz, /dev/sdX, or 'usb'"
    )
