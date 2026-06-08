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
    _fs_tool,
    _resolve_packages,
    _suggest_install,
    check_tools_fs,
    check_tools_gpt,
    check_tools_img,
    check_tools_iso,
    check_tools_mbr,
    _detect_pkg_manager,
)
from mkimage.usb.detect import MAX_USB_SIZE_GB, _list_removable_drives
from mkimage.usb.write import (
    _clone_usb_to_image,
    _clone_usb_to_usb,
    _write_usb_from_dir,
    _write_usb_from_image,
)


def _parse_partition_spec(spec: str) -> PartitionSpec:
    """Parse a TYPE:SIZE:LABEL[:DIR] string into a PartitionSpec."""
    parts = spec.split(":", 3)
    return PartitionSpec(
        fs_type=parts[0] if len(parts) > 0 and parts[0] else "fat32",
        size=parts[1] if len(parts) > 1 else "",
        label=parts[2] if len(parts) > 2 and parts[2] else "UEFITOOLS",
        source_dir=parts[3] if len(parts) > 3 else "",
    )


def _powershell_available() -> bool:
    """True if a PowerShell interpreter is on PATH (Windows native GUI host).

    Uses shutil.which directly, NOT platform._which — the latter wraps probes
    in `wsl bash -c "which ..."` on Windows and would never find powershell.exe.
    Only consulted on Windows (callers gate on _is_windows()).
    """
    import shutil
    return bool(shutil.which("powershell") or shutil.which("pwsh"))


def _resolve_gui_choice(args: argparse.Namespace) -> str:
    """Decide which GUI to launch: 'native' (PowerShell WinForms) or 'python'
    (Dear PyGui / Tkinter).

    Explicit flags always win (--native-gui / --python-gui). Otherwise the
    default is 'native' on Windows when a PowerShell interpreter is present,
    and 'python' everywhere else.
    """
    if args.native_gui:
        return "native"
    if args.python_gui:
        return "python"
    # On Windows the native PowerShell WinForms GUI is the default (when a
    # PowerShell interpreter is present); --python-gui overrides it. Other
    # platforms always use the cross-platform Dear PyGui interface.
    if _is_windows() and _powershell_available():
        return "native"
    return "python"


def _launch_native_gui() -> bool:
    """Launch the native PowerShell WinForms GUI (mkimage.ps1, no -Action).

    Windows-only. Returns True if the GUI was launched (and has since been
    closed), False if it could not run — in which case the caller falls back
    to the cross-platform Python GUI.
    """
    if not _is_windows():
        print("--native-gui is only available on Windows.")
        return False

    from mkimage.platform import _find_ps1

    ps1 = _find_ps1()
    if not ps1:
        print("Could not locate mkimage.ps1 for the native GUI.")
        return False

    import subprocess

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", ps1],
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Failed to launch the native GUI: {exc}")
        return False
    return True


def main() -> None:
    # Make output robust regardless of the platform's default stream encoding.
    # On Windows a piped stdout defaults to the legacy console code page
    # (cp1252), which raises UnicodeEncodeError on any non-ASCII byte — e.g.
    # subprocess/diskpart output relayed through cfg.log. UTF-8 + replace keeps
    # the tool from crashing mid-write on a stray character.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

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

  # UDF bridge ISO (supports files >4GB):
  %(prog)s --source build/ --target boot.iso --udf-bridge

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

  # USB with persistent storage partition (4GB ext4):
  %(prog)s --source build/ --target usb --persistent 4G

  # Custom cluster size for FAT32:
  %(prog)s --source build/ --target boot.img --cluster-size 32768

  # Clone USB to image file:
  %(prog)s --source /dev/sdb --target backup.img

  # Clone USB to compressed image:
  %(prog)s --source /dev/sdb --target backup.img.gz

  # Clone USB to another USB:
  %(prog)s --source /dev/sdb --target /dev/sdc

  # Check USB drive for bad blocks:
  %(prog)s --check-usb /dev/sdb

  # List contents of an image:
  %(prog)s --list-image boot.img

  # List USB drives:
  %(prog)s --list-drives

  # Check tool availability:
  %(prog)s --check

partition spec format:
  --partition TYPE:SIZE:LABEL[:SOURCE_DIR]
    TYPE:  esp, fat32, exfat, ntfs
    SIZE:  64M (fixed), 4G (fixed), +32M (content + extra),
           0 (rest of disk), empty (auto-sized)
    LABEL: volume label (11 chars max for FAT)
    DIR:   optional source directory (default: use --source)

tips:
  - Works natively on Windows (no WSL needed) — uses PowerShell
  - FAT32 images (.img) don't need root on Linux; GPT/MBR do
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
        help="Partition spec (repeatable). TYPE: esp/fat32/exfat/ntfs/ext4/udf, "
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
        "--udf-bridge", action="store_true",
        help="Create UDF bridge ISO (ISO 9660 + UDF, supports files >4GB)",
    )
    img_group.add_argument(
        "--verify", action="store_true",
        help="SHA256 verification after build",
    )
    img_group.add_argument(
        "--cluster-size", type=int, default=0, metavar="BYTES",
        help="Cluster size in bytes for FAT32 (e.g. 4096, 32768). Default: auto",
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
    usb_group.add_argument(
        "--wipe", metavar="DEVICE",
        help="Wipe all partition signatures from a device (MBR, GPT, filesystem)",
    )
    usb_group.add_argument(
        "--check-usb", metavar="DEVICE",
        help="Check USB drive for bad blocks (destructive test, erases all data)",
    )
    usb_group.add_argument(
        "--format", metavar="DEVICE",
        help="Format a USB drive. Uses --gpt/--mbr, --partition, --label for configuration.",
    )
    usb_group.add_argument(
        "--persistent", metavar="SIZE",
        help="Add persistent storage partition (e.g. 4G). Creates ext4 'casper-rw' partition.",
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

    mod_group.add_argument(
        "--list-image", metavar="IMAGE",
        help="List contents of a FAT32 disk image (.img) and exit",
    )

    # --- General ---
    gen_group = parser.add_argument_group("General")
    gen_group.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show per-file output and transfer progress",
    )
    gen_group.add_argument(
        "--gui", action="store_true",
        help="Launch the graphical interface (native PowerShell GUI on "
             "Windows, Dear PyGui/Tkinter elsewhere)",
    )
    gen_group.add_argument(
        "--native-gui", action="store_true",
        help="On Windows, launch the native PowerShell WinForms GUI instead "
             "of the cross-platform Dear PyGui interface",
    )
    gen_group.add_argument(
        "--python-gui", action="store_true",
        help="Force the cross-platform Dear PyGui interface (overrides the "
             "native GUI where it is the default)",
    )
    gen_group.add_argument(
        "--check", action="store_true",
        help="Check tool availability and exit",
    )

    args = parser.parse_args()

    if args.gui or args.native_gui or args.python_gui or len(sys.argv) == 1:
        if _resolve_gui_choice(args) == "native":
            if _launch_native_gui():
                return
            print("Native GUI unavailable; falling back to the Python GUI.")
        try:
            from mkimage.gui_dpg import gui_main
        except ImportError:
            # Try to install dearpygui automatically
            import subprocess as _sp
            print("Dear PyGui not found. Attempting install...")
            r = _sp.run([sys.executable, "-m", "pip", "install",
                         "dearpygui", "--quiet"],
                        capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                try:
                    from mkimage.gui_dpg import gui_main
                    print("Dear PyGui installed successfully.")
                except ImportError:
                    print("Install succeeded but import failed. Using Tkinter.")
                    from mkimage.gui_tk import gui_main
            else:
                print("Could not install Dear PyGui. Using Tkinter.")
                from mkimage.gui_tk import gui_main
        gui_main()
        return

    source = args.source
    target = args.target

    # Build partitions list from --partition specs
    partitions = [_parse_partition_spec(s) for s in args.partition]
    # Infer --gpt if any partition is esp type
    if any(p.fs_type == "esp" for p in partitions):
        args.gpt = True

    # --persistent shorthand: add ext4 casper-rw partition
    if args.persistent:
        args.gpt = True
        partitions.append(PartitionSpec("ext4", args.persistent, "casper-rw"))

    # If no --partition flags, create default partition with --label
    if not partitions:
        p = PartitionSpec()
        p.label = args.label
        if args.cluster_size > 0:
            p.cluster_size = args.cluster_size
        partitions.append(p)
    elif args.cluster_size > 0:
        partitions[0].cluster_size = args.cluster_size

    cfg = Config(
        verbose=args.verbose,
        verify=args.verify,
        label=args.label,
        gpt=args.gpt,
        mbr=args.mbr,
        force=args.force,
        iso_hybrid=args.iso_hybrid,
        udf_bridge=args.udf_bridge,
        partitions=partitions,
    )

    # --list-image operation
    if args.list_image:
        from mkimage.inspect import list_image
        list_image(args.list_image)
        return

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

    if args.wipe:
        from mkimage.usb.safety import _wipe_device, _unmount_device, _verify_usb_bus
        device = args.wipe
        if not _verify_usb_bus(cfg, device):
            print(f"Error: {device} is not on the USB bus. Refusing.", file=sys.stderr)
            sys.exit(1)
        if not cfg.force:
            print(f"\n  WARNING: This will ERASE ALL DATA on {device}.\n")
            try:
                confirm = input(f"  Type 'yes' to wipe {device}: ").strip()
            except (EOFError, KeyboardInterrupt):
                confirm = ""
            if confirm != "yes":
                print("Aborted.")
                sys.exit(0)
        _unmount_device(cfg, device)
        _wipe_device(cfg, device)
        print(f"[OK] Wiped all partition signatures from {device}.")
        sys.exit(0)

    if args.check_usb:
        from mkimage.usb.safety import _check_bad_blocks, _verify_usb_bus, _unmount_device
        device = args.check_usb
        if not _verify_usb_bus(cfg, device):
            print(f"Error: {device} is not on the USB bus. Refusing.", file=sys.stderr)
            sys.exit(1)
        if not cfg.force:
            print(f"\n  WARNING: Bad block test will ERASE ALL DATA on {device}.\n")
            try:
                confirm = input(f"  Type 'yes' to test {device}: ").strip()
            except (EOFError, KeyboardInterrupt):
                confirm = ""
            if confirm != "yes":
                print("Aborted.")
                sys.exit(0)
        _unmount_device(cfg, device)
        if _check_bad_blocks(cfg, device):
            print(f"[OK] No bad blocks found on {device}.")
        else:
            print(f"[FAIL] Bad blocks detected on {device}!", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if args.format:
        from mkimage.usb.write import format_device
        from mkimage.usb.safety import _verify_usb_bus, _unmount_device
        device = args.format
        if not _verify_usb_bus(cfg, device):
            print(f"Error: {device} is not on the USB bus. Refusing.", file=sys.stderr)
            sys.exit(1)
        if not cfg.force:
            scheme = "GPT" if cfg.gpt else ("MBR" if cfg.mbr else "raw")
            fs = cfg.partitions[0].fs_type if cfg.partitions else "fat32"
            print(f"\n  WARNING: This will ERASE ALL DATA on {device}.")
            print(f"  Format: {fs} ({scheme})\n")
            try:
                confirm = input(f"  Type 'yes' to format {device}: ").strip()
            except (EOFError, KeyboardInterrupt):
                confirm = ""
            if confirm != "yes":
                print("Aborted.")
                sys.exit(0)
        _unmount_device(cfg, device)
        format_device(cfg, device)
        print(f"[OK] Formatted {device}.")
        sys.exit(0)

    if args.check:
        img_missing = check_tools_img()
        iso_missing = check_tools_iso()
        mbr_missing = check_tools_mbr()
        gpt_missing = check_tools_gpt()
        if _is_windows():
            env = "Windows (native)"
        else:
            env = "native"
        print(f"Environment: {env}")
        print(f"FAT32 (.img): {'OK' if not img_missing else 'MISSING: ' + ', '.join(img_missing)}")
        print(f"ISO   (.iso): {'OK' if not iso_missing else 'MISSING: ' + ', '.join(iso_missing)}")
        print(f"MBR   (.img): {'OK' if not mbr_missing else 'MISSING: ' + ', '.join(mbr_missing)}")
        print(f"GPT   (.img): {'OK' if not gpt_missing else 'MISSING: ' + ', '.join(gpt_missing)}")
        print(f"\nFilesystems:")
        for fs_name in ["fat32", "exfat", "ntfs", "ext4", "udf"]:
            fs_missing = check_tools_fs(fs_name)
            tool = _fs_tool(fs_name)
            if not fs_missing:
                print(f"  {fs_name:8s} OK ({tool})")
            else:
                pkgs = _resolve_packages(fs_missing)
                # If the "package" is just the raw tool name, there's no real package
                if pkgs == fs_missing:
                    print(f"  {fs_name:8s} N/A (not available on this platform)")
                else:
                    hint = _suggest_install(fs_missing[0])
                    print(f"  {fs_name:8s} MISSING ({fs_missing[0]} -- {hint})")
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
                _write_usb_from_dir(cfg, files, target, source, args.include or [])

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

        elif source_type in ("device", "usb-auto"):
            if target_type in ("device", "usb-auto"):
                print(f"Cloning {source} to USB...")
                _clone_usb_to_usb(cfg, source, target)
            elif target_type in ("img",):
                compressed = _is_compressed_path(target)
                print(f"Cloning {source} to {target}...")
                _clone_usb_to_image(cfg, source, target)
            else:
                print(f"Error: cannot clone device to '{target}'",
                      file=sys.stderr)
                sys.exit(1)
            print("Done.")

    except (RuntimeError, FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
