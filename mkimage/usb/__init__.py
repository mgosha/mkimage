"""USB drive operations: detection, safety, and write."""
from __future__ import annotations

from mkimage.usb.detect import (
    MAX_USB_SIZE_GB,
    _list_removable_drives,
    _list_removable_drives_linux,
    _list_removable_drives_macos,
    _list_removable_drives_windows,
)
from mkimage.usb.safety import (
    _check_bad_blocks,
    _cli_confirm_write,
    _cli_select_drive,
    _resolve_usb_target,
    _unmount_device,
    _usb_safety_checks,
    _verify_usb_bus,
)
from mkimage.usb.write import (
    _clone_usb_to_image,
    _clone_usb_to_usb,
    _extract_iso_to_usb,
    format_device,
    _is_hybrid_iso,
    _poll_progress,
    _write_gpt_to_device,
    _write_usb_from_dir,
    _write_usb_from_image,
    _write_usb_linux,
    _write_usb_windows,
    write_usb,
)

__all__ = [
    "MAX_USB_SIZE_GB",
    "_list_removable_drives",
    "_list_removable_drives_linux",
    "_list_removable_drives_macos",
    "_list_removable_drives_windows",
    "_check_bad_blocks",
    "_cli_confirm_write",
    "_cli_select_drive",
    "_resolve_usb_target",
    "_unmount_device",
    "_usb_safety_checks",
    "_verify_usb_bus",
    "_clone_usb_to_image",
    "_clone_usb_to_usb",
    "_extract_iso_to_usb",
    "format_device",
    "_is_hybrid_iso",
    "_poll_progress",
    "_write_gpt_to_device",
    "_write_usb_from_dir",
    "_write_usb_from_image",
    "_write_usb_linux",
    "_write_usb_windows",
    "write_usb",
]
