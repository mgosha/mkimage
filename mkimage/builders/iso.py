"""ISO image builder."""
from __future__ import annotations

import os
import subprocess as _sp
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from mkimage.files import _stage_files
from mkimage.platform import _find_ps1, _find_tool, _is_windows, _resolve, _run, _which
from mkimage.tools import ensure_tools

if TYPE_CHECKING:
    from mkimage import Config


def _build_iso_windows(cfg: Config, files: dict[str, str], output: str) -> None:
    """Create ISO image on Windows via mkimage.ps1 (IMAPI2, no WSL needed)."""
    ps1 = _find_ps1()
    if not ps1:
        raise RuntimeError("mkimage.ps1 not found. Cannot create ISOs on Windows.")

    with tempfile.TemporaryDirectory() as staging:
        _stage_files(files, Path(staging))

        cmd = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", ps1,
            "-Action", "CreateIso",
            "-SourceDir", staging,
            "-OutputFile", str(Path(output).resolve()),
            "-Label", cfg.label[:32],
        ]
        if cfg.verbose:
            cmd.append("-Verbose")

        cfg.log("  Creating ISO via PowerShell...")
        r = _sp.run(cmd, capture_output=True, text=True)
        if r.stdout.strip():
            for line in r.stdout.strip().splitlines():
                cfg.log(f"  {line}")
        if r.returncode != 0:
            err = r.stderr.strip() or r.stdout.strip()
            raise RuntimeError(f"ISO creation failed: {err}")

    actual_size = os.path.getsize(output)
    cfg.log(f"  [OK] Created {output} ({actual_size // 1024}KB, ISO)")


def build_iso(cfg: Config, files: dict[str, str], output: str) -> None:
    """Create an ISO image. Optionally creates a hybrid ISO (dd-writable to USB).

    On Windows, delegates to mkimage.ps1 for native ISO creation.
    """
    # Warn about files exceeding ISO 9660's 4GB per-file limit
    _ISO9660_MAX = 4 * 1024 * 1024 * 1024  # 4 GiB
    for img_path, local_path in files.items():
        try:
            size = os.path.getsize(local_path)
        except OSError:
            continue
        if size >= _ISO9660_MAX:
            size_gb = size / (1024 ** 3)
            cfg.log(f"  WARNING: {img_path} is {size_gb:.1f}GB — exceeds "
                    f"ISO 9660's 4GB file size limit. "
                    f"The resulting ISO may be unreadable.")

    if _is_windows():
        _build_iso_windows(cfg, files, output)
        return

    ensure_tools(cfg, "iso")

    with tempfile.TemporaryDirectory() as staging:
        _stage_files(files, Path(staging))
        stg_resolved = _resolve(staging)
        out = _resolve(output)

        # UDF bridge: prefer genisoimage (xorriso mkisofs mode lacks -udf)
        use_xorriso = _which("xorriso") and not cfg.udf_bridge
        if cfg.udf_bridge and not _which("genisoimage") and _which("xorriso"):
            # No genisoimage — fall back to xorriso without UDF
            cfg.log("  Warning: xorriso does not support -udf in mkisofs mode.")
            cfg.log("  Install genisoimage for UDF bridge support.")
            use_xorriso = True

        if use_xorriso:
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
            giso_cmd = [
                "genisoimage",
                "-o", out,
                "-R", "-J", "-joliet-long",
                "-V", cfg.label[:32],
            ]
            if cfg.udf_bridge:
                giso_cmd.append("-udf")
                cfg.log("  Creating UDF bridge ISO (ISO 9660 + UDF)")
            giso_cmd.append(stg_resolved)
            _run(cfg, giso_cmd, verbose=True)

    actual_size = os.path.getsize(output)
    fmt_desc = "ISO 9660 + UDF" if cfg.udf_bridge else "ISO 9660"
    cfg.log(f"  Created {output} ({actual_size // 1024}KB, {fmt_desc})")
