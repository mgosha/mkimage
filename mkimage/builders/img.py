"""FAT32 image builder — no partition table."""
from __future__ import annotations

import os
import subprocess as _sp
import tempfile
from pathlib import Path, PurePosixPath

from mkimage.platform import _is_windows, _run, _which, _resolve
from mkimage.tools import ensure_tools, _suggest_install
from mkimage.verify import _verify_write


_extracted_ps1: str | None = None


def _find_ps1() -> str | None:
    """Locate mkimage.ps1 — filesystem, zipapp resource, or cwd."""
    global _extracted_ps1
    if _extracted_ps1 and os.path.isfile(_extracted_ps1):
        return _extracted_ps1

    # Check filesystem locations
    candidates = [
        Path(__file__).resolve().parent.parent / "mkimage.ps1",
        Path(__file__).resolve().parent.parent.parent / "mkimage.ps1",
        Path.cwd() / "mkimage.ps1",
    ]
    for p in candidates:
        if p.is_file():
            return str(p)

    # Check if bundled inside zipapp — extract to shared temp location
    # so both normal and elevated processes can access it
    try:
        import importlib.resources as pkg_resources
        ref = pkg_resources.files("mkimage").joinpath("mkimage.ps1")
        data = ref.read_bytes()
        shared_dir = os.path.join(os.environ.get("SystemDrive", "C:"), "temp")
        os.makedirs(shared_dir, exist_ok=True)
        tmp_path = os.path.join(shared_dir, "mkimage.ps1")
        with open(tmp_path, "wb") as f:
            f.write(data)
        _extracted_ps1 = tmp_path
        return tmp_path
    except (ImportError, FileNotFoundError, TypeError, AttributeError):
        pass

    return None


def _stage_files(files: dict[str, str], staging: Path) -> None:
    """Stage files into a temp directory preserving structure."""
    import shutil
    for img_path, local_path in files.items():
        dest = staging / img_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)


def _calculate_content_size(files: dict[str, str]) -> int:
    """Return total content size in MB (ceiling)."""
    total_bytes = sum(os.path.getsize(lp) for lp in files.values())
    return max(1, -(-total_bytes // (1024 * 1024)))


def _interpret_size(size_str: str, content_mb: int) -> int:
    """Interpret a size string into total MB.

    '+32M' means content + 32MB extra.  '100M' means exactly 100MB.
    Empty means content + 32MB.  '0' means content + 32MB.
    Minimum is 40MB (FAT32 requirement).
    """
    from mkimage import _parse_size
    if not size_str or size_str == "0":
        return max(40, content_mb + 32)
    if size_str.startswith("+"):
        extra = _parse_size(size_str[1:]) // (1024 * 1024)
        return max(40, content_mb + extra)
    absolute = _parse_size(size_str) // (1024 * 1024)
    return max(40, absolute)


def _populate_img_mcopy(cfg: object, files: dict[str, str], out: str) -> None:
    """Populate a FAT32 image using mcopy (no root needed)."""
    dirs_created: set[str] = set()
    for img_path in sorted(files.keys()):
        parts = PurePosixPath(img_path).parts
        for i in range(1, len(parts)):
            d = str(PurePosixPath(*parts[:i]))
            if d not in dirs_created:
                _run(cfg, ["mmd", "-i", out, f"::{d}"], check=False)
                dirs_created.add(d)
    for img_path, local_path in sorted(files.items()):
        src = _resolve(local_path)
        _run(cfg, ["mcopy", "-i", out, src, f"::{img_path}"],
             verbose=cfg.verbose)
    cfg.log(f"  Copied {len(files)} files via mcopy")


def _populate_img_mount(
    cfg: object, stg_resolved: str, out: str, size_mb: int, label: str,
) -> bool:
    """Populate a FAT32 image via mount+rsync (requires root)."""
    if cfg.verbose:
        rsync_flags = "-av --no-owner --no-group"
    else:
        rsync_flags = "-a --no-owner --no-group --info=progress2"

    dd_cmd = ["dd", "if=/dev/zero", f"of={out}", "bs=1M", f"count={size_mb}"]
    if cfg.verbose:
        dd_cmd.append("status=progress")
    _run(cfg, dd_cmd, verbose=True)
    _run(cfg, ["mkfs.vfat", "-F", "32", "-n", label, out], verbose=True)

    r = _run(cfg, [
        "bash", "-c",
        f"MNTDIR=$(mktemp -d) && "
        f"mount -o loop '{out}' $MNTDIR && "
        f"rsync {rsync_flags} '{stg_resolved}'/ $MNTDIR/ && "
        f"TOTAL=$(find $MNTDIR -type f | wc -l) && "
        f"echo \"Copied $TOTAL files\" && "
        f"umount $MNTDIR && "
        f"rmdir $MNTDIR"
    ], check=False, verbose=True, as_root=True)
    return r.returncode == 0


def _build_img_windows(cfg: object, files: dict[str, str], output: str) -> None:
    """Create FAT32 image on Windows via mkimage.ps1 (no WSL needed).

    diskpart requires admin for VHD operations. Runs elevated via
    Start-Process -Verb RunAs with a progress file for log output.
    Staging dir is managed manually so it survives the elevated subprocess.
    """
    import shutil
    import time
    from mkimage import PartitionSpec

    ps1 = _find_ps1()
    if not ps1:
        raise RuntimeError("mkimage.ps1 not found. Cannot create images on Windows.")

    # Use a shared location accessible by both normal and elevated processes.
    # The user's %TEMP% may not be accessible from an elevated context.
    shared_tmp = os.path.join(os.environ.get("SystemDrive", "C:"), "temp", "mkimage-build")
    os.makedirs(shared_tmp, exist_ok=True)
    staging = tempfile.mkdtemp(prefix="stg-", dir=shared_tmp)
    progress_file = os.path.join(shared_tmp, f"progress-{os.getpid()}.txt")
    # Ensure progress file exists
    with open(progress_file, "w") as f:
        pass

    try:
        _stage_files(files, Path(staging))
        part = cfg.partitions[0] if cfg.partitions else PartitionSpec()
        content_mb = _calculate_content_size(files)
        size_mb = _interpret_size(part.size, content_mb)

        cfg.log(f"  Image size: {size_mb}MB ({content_mb}MB content + {size_mb - content_mb}MB free)")
        cfg.log(f"  {len(files)} files ({content_mb * 1024}KB) to include")

        # Write a wrapper script that the elevated process will execute.
        # This avoids ArgumentList quoting issues with Start-Process.
        output_resolved = str(Path(output).resolve())
        wrapper = os.path.join(shared_tmp, f"run-{os.getpid()}.ps1")
        verbose_flag = "-Verbose" if cfg.verbose else ""
        with open(wrapper, "w") as wf:
            wf.write(
                f'& "{ps1}" '
                f'-Action CreateImg '
                f'-SourceDir "{staging}" '
                f'-OutputFile "{output_resolved}" '
                f'-Label "{part.label[:11]}" '
                f'-SizeMB {size_mb} '
                f'-ProgressFile "{progress_file}" '
                f'{verbose_flag}\n'
            )

        cfg.log("  Requesting Administrator access (diskpart requires elevation)...")
        proc = _sp.Popen([
            "powershell", "-NoProfile", "-Command",
            f"Start-Process -FilePath 'powershell.exe' "
            f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','{wrapper}' "
            f"-Verb RunAs -Wait"
        ], stdout=_sp.PIPE, stderr=_sp.PIPE)

        lines_read = 0
        while proc.poll() is None:
            time.sleep(0.3)
            if os.path.exists(progress_file):
                try:
                    with open(progress_file, "r", encoding="utf-8",
                              errors="replace") as pf:
                        all_lines = pf.readlines()
                    for line in all_lines[lines_read:]:
                        stripped = line.rstrip()
                        if stripped:
                            cfg.log(f"  {stripped}")
                    lines_read = len(all_lines)
                except OSError:
                    pass

        # Read remaining output
        time.sleep(0.5)
        if os.path.exists(progress_file):
            try:
                with open(progress_file, "r", encoding="utf-8",
                          errors="replace") as pf:
                    all_lines = pf.readlines()
                for line in all_lines[lines_read:]:
                    stripped = line.rstrip()
                    if stripped:
                        cfg.log(f"  {stripped}")
            except OSError:
                pass

        if not os.path.isfile(output):
            raise RuntimeError("Image creation failed: output file not created")

        actual_size = os.path.getsize(output)
        cfg.log(f"  [OK] Created {output} ({actual_size // 1024}KB, FAT32)")

        if cfg.verify:
            _verify_write(cfg, files, output)

    except Exception as exc:
        if "canceled by the user" in str(exc):
            raise RuntimeError("Administrator access denied")
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        for f in (progress_file, wrapper):
            try:
                os.unlink(f)
            except OSError:
                pass


def build_img(cfg: object, files: dict[str, str], output: str) -> None:
    """Create a FAT32 disk image (no partition table).

    Primary method uses mcopy (no root needed). Falls back to mount+rsync
    if mcopy is unavailable and root access is available.
    On Windows, delegates to mkimage.ps1 for native image creation.
    """
    if _is_windows():
        _build_img_windows(cfg, files, output)
        return

    from mkimage import PartitionSpec

    ensure_tools(cfg, "img")
    out = _resolve(output)

    part = cfg.partitions[0] if cfg.partitions else PartitionSpec()
    content_mb = _calculate_content_size(files)
    size_mb = _interpret_size(part.size, content_mb)
    part_label = part.label[:11]
    cfg.log(f"  Image size: {size_mb}MB ({content_mb}MB content + {size_mb - content_mb}MB free)")

    with tempfile.TemporaryDirectory() as staging:
        _stage_files(files, Path(staging))
        stg_resolved = _resolve(staging)

        if _which("mcopy"):
            # Primary: mcopy (no root needed)
            cfg.log(f"  Copying {len(files)} files to image...")
            dd_cmd = ["dd", "if=/dev/zero", f"of={out}", "bs=1M", f"count={size_mb}"]
            if cfg.verbose:
                dd_cmd.append("status=progress")
            _run(cfg, dd_cmd, verbose=True)
            mkfs_cmd = ["mkfs.vfat", "-F", "32", "-n", part_label]
            if part.cluster_size > 0:
                mkfs_cmd.extend(["-s", str(part.cluster_size // 512)])
            mkfs_cmd.append(out)
            _run(cfg, mkfs_cmd, verbose=True)
            _populate_img_mcopy(cfg, files, out)
        elif _which("rsync"):
            # Fallback: mount+rsync (requires root)
            cfg.log("  mcopy not found, using mount+rsync (requires root)...")
            cfg.log(f"  Copying {len(files)} files to image...")
            if not _populate_img_mount(cfg, stg_resolved, out, size_mb, part_label):
                raise RuntimeError(
                    "mount/rsync failed. Install mtools for rootless operation:\n"
                    f"    {_suggest_install('mcopy')}"
                )
        else:
            raise RuntimeError(
                "No file copy tool available. Install mtools:\n"
                f"    {_suggest_install('mcopy')}"
            )

    actual_size = os.path.getsize(output)
    cfg.log(f"  [OK] Created {output} ({actual_size // 1024}KB, FAT32)")

    if cfg.verify:
        _verify_write(cfg, files, output)
