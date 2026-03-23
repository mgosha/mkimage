"""ISO image builder."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from mkimage.files import _stage_files
from mkimage.platform import _find_tool, _resolve, _run, _which
from mkimage.tools import ensure_tools

if TYPE_CHECKING:
    from mkimage import Config


def build_iso(cfg: Config, files: dict[str, str], output: str) -> None:
    """Create an ISO image. Optionally creates a hybrid ISO (dd-writable to USB)."""
    ensure_tools(cfg, "iso")

    with tempfile.TemporaryDirectory() as staging:
        _stage_files(files, Path(staging))
        stg_resolved = _resolve(staging)
        out = _resolve(output)

        if _which("xorriso"):
            cmd = [
                "xorriso", "-as", "mkisofs",
                "-o", out,
                "-R", "-J", "-joliet-long",
                "-V", cfg.label[:32],
            ]
            if cfg.iso_hybrid:
                # Create an EFI boot image (FAT12 with EFI files)
                efi_img = Path(staging) / "efi.img"
                efi_dir = Path(staging) / "EFI"
                if efi_dir.is_dir():
                    # Build a small FAT image containing the EFI directory
                    _run(cfg, ["dd", "if=/dev/zero", f"of={efi_img}",
                               "bs=1M", "count=4"], check=True)
                    _run(cfg, [_find_tool("mkfs.vfat"), "-F", "12",
                               str(efi_img)], check=True)
                    # Copy EFI files into the FAT image
                    for f in sorted(efi_dir.rglob("*")):
                        if f.is_file():
                            rel = str(PurePosixPath(f.relative_to(Path(staging))))
                            parts = PurePosixPath(rel).parts
                            for i in range(1, len(parts)):
                                d = str(PurePosixPath(*parts[:i]))
                                _run(cfg, ["mmd", "-i", str(efi_img),
                                           f"::{d}"], check=False)
                            _run(cfg, ["mcopy", "-i", str(efi_img),
                                       str(f), f"::{rel}"], check=False)

                    cmd.extend([
                        "-eltorito-alt-boot",
                        "-e", "efi.img",
                        "-no-emul-boot",
                        "-isohybrid-gpt-basdat",
                    ])
                    cfg.log("  Creating hybrid ISO (UEFI, dd-writable to USB)")
                else:
                    cfg.log("  Warning: no EFI directory found, skipping hybrid")
            cmd.append(stg_resolved)
            _run(cfg, cmd, verbose=True)
        else:
            if cfg.iso_hybrid:
                cfg.log("  Warning: ISO hybrid requires xorriso (genisoimage does not support it)")
            _run(cfg, [
                "genisoimage",
                "-o", out,
                "-R", "-J", "-joliet-long",
                "-V", cfg.label[:32],
                stg_resolved,
            ], verbose=True)

    actual_size = os.path.getsize(output)
    cfg.log(f"  Created {output} ({actual_size // 1024}KB, ISO 9660)")
