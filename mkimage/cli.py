"""Command-line interface and main() entry point."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from mkimage import Config, PartitionSpec
from mkimage.builders import build_gpt_img, build_img, build_iso
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


def _parse_partition_spec(spec: str) -> PartitionSpec:
    """Parse a TYPE:SIZE:LABEL[:DIR] string into a PartitionSpec."""
    parts = spec.split(":", 3)
    return PartitionSpec(
        fs_type=parts[0] if len(parts) > 0 and parts[0] else "fat32",
        size=parts[1] if len(parts) > 1 else "",
        label=parts[2] if len(parts) > 2 and parts[2] else "UEFITOOLS",
        source_dir=parts[3] if len(parts) > 3 else "",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="mkimage -- Create bootable UEFI media images, ISOs, and USB drives.",
        epilog="""\
examples:
  # Create a FAT32 image from a directory:
  %(prog)s --source build/ --target boot.img

  # Create a GPT image with EFI System Partition:
  %(prog)s --source build/ --target boot.img --gpt

  # GPT with ESP + data partition:
  %(prog)s --source build/ --target boot.img --gpt \\
    --partition esp::ESP --partition fat32:0:DATA:./data/

  # MBR-partitioned image:
  %(prog)s --source build/ --target boot.img --mbr

  # Custom partition spec (type:size:label):
  %(prog)s --source build/ --target boot.img --partition fat32:+64M:TEST

  # ISO image (hybrid for USB boot):
  %(prog)s --source build/ --target boot.iso --iso-hybrid

  # Compressed output:
  %(prog)s --source build/ --target boot.img.gz

  # exFAT filesystem (for files >4GB):
  %(prog)s --source build/ --target boot.img --partition exfat::BIGFILES

  # Write directly to USB (auto-detect drive):
  %(prog)s --source build/ --target usb

  # Write existing image to USB:
  %(prog)s --source boot.img --target usb

  # Modify existing image without rebuild:
  %(prog)s --modify boot.img --add newfile.txt --remove old.txt

  # Verify after build:
  %(prog)s --source build/ --target boot.img --verify

  # List USB drives:
  %(prog)s --list-drives

  # Check tool availability:
  %(prog)s --check

tips:
  - FAT32 images (.img) don't need root; GPT/MBR images do
  - Use .img.gz or .img.xz extension for automatic compression
  - ISO hybrid (--iso-hybrid) makes ISOs dd-writable to USB
  - Volume labels are 11 chars max for FAT32/exFAT
  - The GUI launches with --gui or when run with no arguments
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # --- Source and Target ---
    io_group = parser.add_argument_group("Source and Target")
    io_group.add_argument(
        "--source",
        help="Source directory or image file",
    )
    io_group.add_argument(
        "--target",
        help="Target: .img, .iso, .img.gz, /dev/sdX, or 'usb' for auto-detect",
    )
    io_group.add_argument(
        "--include", action="append", default=[],
        help="Additional file or directory to include (repeatable)",
    )

    # --- Partition Scheme ---
    part_group = parser.add_argument_group("Partition Scheme")
    part_group.add_argument(
        "--partition", action="append", default=[], metavar="TYPE:SIZE:LABEL[:DIR]",
        help="Partition spec (repeatable). TYPE: esp/fat32/exfat/ntfs, "
             "SIZE: 64M/4G/+32M/0/auto, LABEL: volume label, "
             "DIR: optional source directory",
    )
    part_group.add_argument(
        "--mbr", action="store_true",
        help="MBR partition table (legacy BIOS boot)",
    )
    part_group.add_argument(
        "--gpt", action="store_true",
        help="GPT partition table with EFI System Partition (UEFI boot)",
    )

    # --- Image Options ---
    img_group = parser.add_argument_group("Image Options")
    img_group.add_argument(
        "--label", default="UEFITOOLS",
        help="Volume label (default: UEFITOOLS, 11 chars max for FAT32)",
    )
    img_group.add_argument(
        "--iso-hybrid", action="store_true",
        help="Create hybrid ISO (dd-writable to USB)",
    )
    img_group.add_argument(
        "--verify", action="store_true",
        help="SHA256 verification after build",
    )

    # --- USB Options ---
    usb_group = parser.add_argument_group("USB Options")
    usb_group.add_argument(
        "--force", action="store_true",
        help="Skip USB write confirmation (safety checks still run)",
    )
    usb_group.add_argument(
        "--list-drives", action="store_true",
        help="List removable USB drives and exit",
    )

    # --- Modify ---
    mod_group = parser.add_argument_group("Image Modification")
    mod_group.add_argument(
        "--modify", metavar="IMAGE",
        help="Modify existing FAT32 image (no rebuild)",
    )
    mod_group.add_argument(
        "--add", action="append", default=[],
        help="File/directory to add (with --modify, repeatable)",
    )
    mod_group.add_argument(
        "--remove", action="append", default=[],
        help="File to remove (with --modify, repeatable)",
    )

    # --- General ---
    gen_group = parser.add_argument_group("General")
    gen_group.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show per-file output and transfer progress",
    )
    gen_group.add_argument(
        "--gui", action="store_true",
        help="Launch graphical interface (Dear PyGui or Tkinter)",
    )
    gen_group.add_argument(
        "--check", action="store_true",
        help="Check tool availability and exit",
    )

    # --- Hidden backward compat flags ---
    parser.add_argument("source_dir", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("-o", "--output", help=argparse.SUPPRESS)
    parser.add_argument("--write-usb", metavar="IMAGE", help=argparse.SUPPRESS)
    parser.add_argument("--extra", type=int, default=32, dest="extra_mb",
                        help=argparse.SUPPRESS)
    parser.add_argument("--fs", default="fat32", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", default="", help=argparse.SUPPRESS)
    parser.add_argument("--data-size", default="", help=argparse.SUPPRESS)
    parser.add_argument("--esp-label", default="ESP", help=argparse.SUPPRESS)
    parser.add_argument("--data-label", default="DATA", help=argparse.SUPPRESS)

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

    # Build partitions list from --partition or legacy flags
    if args.partition:
        partitions = [_parse_partition_spec(s) for s in args.partition]
        # Infer --gpt if any partition is esp type
        if any(p.fs_type == "esp" for p in partitions):
            args.gpt = True
    elif args.gpt and args.data_dir:
        partitions = [
            PartitionSpec("esp", "", args.esp_label or "ESP"),
            PartitionSpec(args.fs, args.data_size, args.data_label or "DATA",
                          args.data_dir),
        ]
    elif args.gpt:
        partitions = [PartitionSpec("esp", "", args.esp_label or "ESP")]
    elif args.mbr:
        partitions = [PartitionSpec(args.fs, "", args.label)]
    else:
        partitions = [PartitionSpec(args.fs, f"+{args.extra_mb}M", args.label)]

    cfg = Config(
        verbose=args.verbose,
        verify=args.verify,
        label=args.label,
        gpt=args.gpt or bool(args.data_dir),
        mbr=args.mbr,
        force=args.force,
        iso_hybrid=args.iso_hybrid,
        partitions=partitions,
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

            if target_type == "img" and cfg.gpt:
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
