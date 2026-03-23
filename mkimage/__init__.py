"""mkimage -- Create bootable UEFI media images from a directory.

Generates FAT32 disk images (.img) or ISO images (.iso) containing
UEFI applications. Runs natively on Linux/macOS or via WSL on Windows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


# ---------------------------------------------------------------------------
# Configuration (core data structure everything depends on)
# ---------------------------------------------------------------------------

@dataclass
class PartitionSpec:
    """Specification for a single partition."""
    fs_type: str = "fat32"    # fat32, exfat, ntfs, ext4, udf, esp (= fat32 + EF00)
    size: str = ""            # 64M, 4G, +32M (extra), 0 (rest), "" (auto)
    label: str = "UEFITOOLS"
    source_dir: str = ""      # "" = use main --source
    cluster_size: int = 0     # cluster size in bytes (0 = auto/default)


@dataclass
class Config:
    """Runtime configuration threaded through all mkimage operations."""
    verbose: bool = False
    verify: bool = False
    gpt: bool = False
    mbr: bool = False
    label: str = "UEFITOOLS"    # ISO label + default partition label
    force: bool = False
    log: Callable[..., None] = field(default=print)
    iso_hybrid: bool = False
    udf_bridge: bool = False
    partitions: list[PartitionSpec] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Re-exports: public API surface
# ---------------------------------------------------------------------------

from mkimage.platform import (  # noqa: E402
    _is_windows,
    _is_macos,
    _run,
    _which,
    _resolve,
    _find_tool,
    _find_ps1,
    _shell_quote,
    _wsl_path,
)
from mkimage.files import (  # noqa: E402
    collect_files,
    _stage_files,
    _calculate_content_size,
    _parse_size,
    _interpret_size,
)
from mkimage.tools import (  # noqa: E402
    check_tools_img,
    check_tools_iso,
    check_tools_gpt,
    check_tools_mbr,
    check_tools_fs,
    get_available_filesystems,
    ensure_tools,
    _suggest_install,
    _resolve_packages,
    _detect_pkg_manager,
    _install_packages,
    _TOOL_PACKAGES,
)
from mkimage.partition import (  # noqa: E402
    _format_partition,
    _setup_loop_device,
    _wait_for_partition,
    _teardown_loop_device,
    _check_root,
    _populate_partition,
)
from mkimage.compress import (  # noqa: E402
    _compress_file,
    _decompress_pipe_cmd,
    _is_compressed_path,
    _strip_compression_ext,
)
from mkimage.verify import _verify_write  # noqa: E402
from mkimage.modify import modify_img  # noqa: E402
from mkimage.detect import _detect_source_type, _detect_target_type  # noqa: E402

# Builders (imported via builders __init__)
from mkimage.builders import (  # noqa: E402
    build_img,
    build_iso,
    build_mbr_img,
    build_gpt_img,
)

# USB operations
from mkimage.usb import (  # noqa: E402
    write_usb,
    _list_removable_drives,
    _clone_usb_to_image,
    _clone_usb_to_usb,
    _write_usb_from_dir,
    _write_usb_from_image,
    _is_hybrid_iso,
    _extract_iso_to_usb,
)
from mkimage.usb.detect import MAX_USB_SIZE_GB  # noqa: E402
from mkimage.usb.safety import (  # noqa: E402
    _verify_usb_bus,
    _usb_safety_checks,
    _unmount_device,
    _resolve_usb_target,
    _cli_select_drive,
    _cli_confirm_write,
    _wipe_device,
    _check_bad_blocks,
)

# CLI entry point
from mkimage.cli import main  # noqa: E402

__all__ = [
    "Config",
    "PartitionSpec",
    # platform
    "_is_windows", "_is_macos", "_run", "_which", "_resolve", "_find_tool",
    "_find_ps1", "_shell_quote", "_wsl_path",
    # files
    "collect_files", "_stage_files", "_calculate_content_size", "_parse_size",
    "_interpret_size",
    # tools
    "check_tools_img", "check_tools_iso", "check_tools_gpt", "check_tools_mbr",
    "check_tools_fs", "ensure_tools", "_suggest_install", "_resolve_packages",
    "_detect_pkg_manager", "_install_packages", "_TOOL_PACKAGES",
    # partition
    "_format_partition", "_setup_loop_device", "_wait_for_partition",
    "_teardown_loop_device", "_check_root", "_populate_partition",
    # compress
    "_compress_file", "_decompress_pipe_cmd", "_is_compressed_path",
    "_strip_compression_ext",
    # verify
    "_verify_write",
    # modify
    "modify_img",
    # detect
    "_detect_source_type", "_detect_target_type",
    # builders
    "build_img", "build_iso", "build_mbr_img", "build_gpt_img",
    # usb
    "write_usb", "_list_removable_drives",
    "_clone_usb_to_image", "_clone_usb_to_usb",
    "_write_usb_from_dir", "_write_usb_from_image",
    "_is_hybrid_iso", "_extract_iso_to_usb",
    "_verify_usb_bus", "_usb_safety_checks", "_unmount_device", "_resolve_usb_target",
    "_cli_select_drive", "_cli_confirm_write", "_check_bad_blocks",
    # cli
    "main",
]
