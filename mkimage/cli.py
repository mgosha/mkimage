"""Command-line interface and main() entry point."""
from __future__ import annotations

import argparse
import os
import sys

from mkimage import Config
from mkimage.builders import build_gpt_data_img, build_gpt_img, build_img, build_iso
from mkimage.builders.mbr import build_mbr_img
from mkimage.compress import _compress_file, _is_compressed_path, _strip_compression_ext
from mkimage.detect import _detect_source_type, _detect_target_type
from mkimage.files import collect_files
from mkimage.modify import modify_img
from mkimage.platform import _is_windows
from mkimage.tools import (
    _resolve_packages,
    check_tools_gpt,
    check_tools_img,
    check_tools_iso,
    check_tools_mbr,
    _detect_pkg_manager,
)
from mkimage.usb.detect import MAX_USB_SIZE_GB, _list_removable_drives
from mkimage.usb.write import _write_usb_from_dir, _write_usb_from_image


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create bootable UEFI media images from a directory.",
        epilog="""\
examples:
  %(prog)s --source build/ --target boot.img
  %(prog)s --source build/ --target boot.img --gpt
  %(prog)s --source build/ --target boot.iso
  %(prog)s --source build/ --target /dev/sdb
  %(prog)s --source build/ --target usb
  %(prog)s --source boot.img --target usb
  %(prog)s --list-drives
  %(prog)s --check

  # Backward-compatible syntax:
  %(prog)s build/ -o boot.img
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # New --source/--target interface
    parser.add_argument(
        "--source",
        help="Source directory or image file",
    )
    parser.add_argument(
        "--target",
        help="Target: .img file, .iso file, /dev/sdX device, or 'usb' for auto-detect",
    )
    # Backward-compatible positional and -o
    parser.add_argument(
        "source_dir", nargs="?",
        help=argparse.SUPPRESS,  # hidden, use --source instead
    )
    parser.add_argument(
        "-o", "--output",
        help=argparse.SUPPRESS,  # hidden, use --target instead
    )
    # Common options
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
        "--mbr", action="store_true",
        help="Create MBR-partitioned image",
    )
    parser.add_argument(
        "--gpt", action="store_true",
        help="Create GPT image with EFI System Partition",
    )
    parser.add_argument(
        "--data-dir",
        help="Directory for second data partition (implies --gpt)",
    )
    parser.add_argument(
        "--data-size", default="",
        help="Fixed size for data partition (e.g. 512M, 4G). Default: auto",
    )
    parser.add_argument(
        "--esp-label", default="ESP",
        help="ESP volume label (default: ESP)",
    )
    parser.add_argument(
        "--data-label", default="DATA",
        help="Data partition volume label (default: DATA)",
    )
    parser.add_argument(
        "--fs", default="fat32", choices=["fat32", "exfat", "ntfs"],
        help="Filesystem type (default: fat32)",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="Verify files after writing (SHA256 comparison)",
    )
    parser.add_argument(
        "--iso-hybrid", action="store_true",
        help="Create hybrid ISO that can be dd'd to USB",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Skip USB write confirmation prompt",
    )
    parser.add_argument(
        "--list-drives", action="store_true",
        help="List removable USB drives and exit",
    )
    parser.add_argument(
        "--modify", metavar="IMAGE",
        help="Modify existing FAT32 image (add/remove files)",
    )
    parser.add_argument(
        "--add", action="append", default=[],
        help="File or directory to add (with --modify, repeatable)",
    )
    parser.add_argument(
        "--remove", action="append", default=[],
        help="File to remove from image (with --modify, repeatable)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show per-file output and transfer progress",
    )
    parser.add_argument(
        "--gui", action="store_true",
        help="Launch graphical interface",
    )
    # Backward compat: --write-usb IMAGE (old syntax)
    parser.add_argument(
        "--write-usb", metavar="IMAGE",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Check tool availability and exit",
    )

    args = parser.parse_args()

    if args.gui or len(sys.argv) == 1:
        try:
            from mkimage.gui_dpg import gui_main
        except ImportError:
            from mkimage.gui_tk import gui_main
        gui_main()
        return

    # Resolve --source / --target from new or old-style arguments
    source = args.source or args.source_dir
    target = args.target or args.output

    # Backward compat: --write-usb IMAGE -> --source IMAGE --target usb
    if args.write_usb:
        source = source or args.write_usb
        target = target or "usb"

    cfg = Config(
        verbose=args.verbose,
        verify=args.verify,
        label=args.label,
        extra_mb=args.extra_mb,
        gpt=args.gpt or bool(args.data_dir),
        mbr=args.mbr,
        force=args.force,
        data_dir=args.data_dir or "",
        data_size=args.data_size,
        esp_label=args.esp_label,
        data_label=args.data_label,
        iso_hybrid=args.iso_hybrid,
        fs_type=args.fs,
    )

    # --modify operation
    if args.modify:
        try:
            modify_img(cfg, args.modify, args.add, args.remove)
        except (RuntimeError, FileNotFoundError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.list_drives:
        drives = _list_removable_drives()
        if not drives:
            print("No removable USB drives found.")
        else:
            print(f"Removable USB drives (<={MAX_USB_SIZE_GB}GB):")
            for d in drives:
                model = f"  {d['model']}" if d['model'] else ""
                print(f"  {d['path']}  {d['size']}{model}")
        sys.exit(0)

    if args.check:
        img_missing = check_tools_img()
        iso_missing = check_tools_iso()
        mbr_missing = check_tools_mbr()
        gpt_missing = check_tools_gpt()
        env = "WSL" if _is_windows() else "native"
        print(f"Environment: {env}")
        print(f"FAT32 (.img): {'OK' if not img_missing else 'MISSING: ' + ', '.join(img_missing)}")
        print(f"ISO   (.iso): {'OK' if not iso_missing else 'MISSING: ' + ', '.join(iso_missing)}")
        print(f"MBR   (.img): {'OK' if not mbr_missing else 'MISSING: ' + ', '.join(mbr_missing)}")
        print(f"GPT   (.img): {'OK' if not gpt_missing else 'MISSING: ' + ', '.join(gpt_missing)}")
        all_missing = sorted(set(img_missing + iso_missing + mbr_missing + gpt_missing))
        if all_missing:
            packages = _resolve_packages(all_missing)
            pkg_cmd, _ = _detect_pkg_manager()
            if pkg_cmd:
                print(f"\nInstall all: sudo {pkg_cmd} install {' '.join(packages)}")
        sys.exit(1 if all_missing else 0)

    if not source:
        parser.error("--source (or positional source_dir) is required")
    if not target:
        parser.error("--target (or -o/--output) is required")

    try:
        source_type = _detect_source_type(source)
        target_type = _detect_target_type(target)

        if source_type == "directory":
            print(f"Collecting files from {source}...")
            files = collect_files(cfg, source, args.include)
            if not files:
                print("Error: no files found", file=sys.stderr)
                sys.exit(1)
            print(f"  {len(files)} files, "
                  f"{sum(os.path.getsize(p) for p in files.values()) // 1024}KB total")
            for img_path in sorted(files.keys()):
                print(f"    {img_path}")

            # Determine if output needs compression
            compressed = _is_compressed_path(target)
            if compressed:
                build_target = _strip_compression_ext(target)
            else:
                build_target = target

            if target_type == "img" and cfg.gpt and cfg.data_dir:
                data_files = collect_files(cfg, cfg.data_dir, [])
                print(f"  {len(data_files)} data files, "
                      f"{sum(os.path.getsize(p) for p in data_files.values()) // 1024}KB")
                print("Building GPT image (ESP + data)...")
                build_gpt_data_img(cfg, files, data_files, build_target)
            elif target_type == "img" and cfg.gpt:
                print("Building GPT image...")
                build_gpt_img(cfg, files, build_target)
            elif target_type == "img" and cfg.mbr:
                print("Building MBR image...")
                build_mbr_img(cfg, files, build_target)
            elif target_type == "img":
                print("Building image (no partition table)...")
                build_img(cfg, files, build_target)
            elif target_type == "iso":
                print("Building ISO image...")
                build_iso(cfg, files, build_target)
            elif target_type in ("device", "usb-auto"):
                _write_usb_from_dir(cfg, files, target)

            if compressed and target_type not in ("device", "usb-auto"):
                _compress_file(cfg, build_target, target)
                os.unlink(build_target)

            print("Done.")

        elif source_type == "image":
            if target_type in ("device", "usb-auto"):
                print(f"Writing {source} to USB...")
                _write_usb_from_image(cfg, source, target)
                print("Done.")
            else:
                print(f"Error: cannot write image to file target '{target}'",
                      file=sys.stderr)
                sys.exit(1)

    except (RuntimeError, FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
