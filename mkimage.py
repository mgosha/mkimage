#!/usr/bin/env python3
"""
mkimage — Create bootable UEFI media images from a directory.

Generates FAT32 disk images (.img) or ISO images (.iso) containing
UEFI applications. Runs natively on Linux/macOS or via WSL on Windows.

Usage:
    # From a directory of .efi files:
    python mkimage.py build/binaries/ -o ipmitool.img
    python mkimage.py build/binaries/ -o ipmitool.iso

    # Include extra files:
    python mkimage.py build/binaries/ --include scripts/xfer-server.py -o ipmitool.img

    # Set volume label:
    python mkimage.py build/binaries/ -o ipmitool.img --label IPMITOOL

    # Specify image size (MB, for .img only):
    python mkimage.py build/binaries/ -o ipmitool.img --size 64

    # Write an image to a USB flash drive:
    python mkimage.py --write-usb ipmitool.img

Requirements:
    - Linux/macOS: mtools (mcopy, mmd, mkfs.vfat), xorriso or genisoimage
    - Windows: WSL with the above tools installed

Python 3.7+ — no external packages required.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """Runtime configuration threaded through all mkimage operations."""
    verbose: bool = False
    verify: bool = False
    gpt: bool = False
    label: str = "UEFITOOLS"
    extra_mb: int = 32
    force: bool = False
    log: Callable[..., None] = field(default=print)
    data_dir: str = ""
    data_size: str = ""
    esp_label: str = "ESP"
    data_label: str = "DATA"
    iso_hybrid: bool = False
    fs_type: str = "fat32"


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def _is_windows() -> bool:
    return platform.system() == "Windows"


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _wsl_path(win_path: str) -> str:
    """Convert a Windows path to a WSL /mnt/c/... path."""
    p = Path(win_path).resolve()
    drive = p.drive.rstrip(":").lower()
    rest = str(p).replace(p.drive, "").replace("\\", "/")
    return f"/mnt/{drive}{rest}"


def _run(cfg: Config, cmd: list[str], check: bool = True,
         verbose: bool = False,
         as_root: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a command, routing through WSL on Windows.

    Args:
        cfg: Runtime configuration (used for logging).
        verbose: Log this specific command's invocation and output.
        as_root: Run as root. On Windows uses 'wsl -u root'. On Linux uses 'sudo'.
    """
    # On macOS, resolve tool paths (Homebrew sbin may not be in PATH)
    if _is_macos() and cmd and not cmd[0].startswith("/"):
        cmd = [_find_tool(cmd[0])] + cmd[1:]

    if _is_windows():
        shell_cmd = " ".join(_shell_quote(c) for c in cmd)
        if as_root:
            actual = ["wsl", "-u", "root", "bash", "-c", shell_cmd]
        else:
            actual = ["wsl", "bash", "-c", shell_cmd]
    else:
        if as_root:
            actual = ["sudo"] + cmd
        else:
            actual = cmd
    if verbose:
        prefix = "(root) " if as_root else ""
        cfg.log(f"  > {prefix}{' '.join(cmd)}")
    result = subprocess.run(actual, check=check, capture_output=True, text=True)
    if verbose and result.stdout.strip():
        for line in result.stdout.strip().splitlines():
            cfg.log(f"  {line}")
    if verbose and result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            cfg.log(f"  {line}")
    return result


def _shell_quote(s: str) -> str:
    """Shell-quote a string for bash."""
    if all(c.isalnum() or c in "-_./=:" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def _which(tool: str) -> bool:
    """Check if a tool is available (via WSL on Windows)."""
    _quiet = Config()  # silent config for tool probing
    try:
        r = _run(_quiet, ["which", tool], check=False)
        if r.returncode == 0:
            return True
    except FileNotFoundError:
        pass
    # On macOS, Homebrew sbin may not be in PATH
    if _is_macos():
        for sbin in ["/usr/local/sbin", "/opt/homebrew/sbin"]:
            if Path(f"{sbin}/{tool}").exists():
                return True
    return False


def _find_tool(tool: str) -> str:
    """Find a tool's full path, checking Homebrew sbin on macOS."""
    if _is_macos():
        for sbin in ["/usr/local/sbin", "/opt/homebrew/sbin",
                     "/usr/local/bin", "/opt/homebrew/bin"]:
            full = f"{sbin}/{tool}"
            if Path(full).exists():
                return full
    return tool  # return as-is, let PATH handle it


def _resolve(path: str) -> str:
    """Resolve a path for the execution environment (WSL or native)."""
    if _is_windows():
        return _wsl_path(path)
    return str(Path(path).resolve())


# ---------------------------------------------------------------------------
# Tool checks and auto-install
# ---------------------------------------------------------------------------

# Map tool names to packages per distro family
_TOOL_PACKAGES: dict[str, dict[str, str]] = {
    "mkfs.vfat": {"apt": "dosfstools",  "dnf": "dosfstools",  "pacman": "dosfstools",  "brew": "dosfstools"},
    "mcopy":     {"apt": "mtools",      "dnf": "mtools",      "pacman": "mtools",      "brew": "mtools"},
    "mmd":       {"apt": "mtools",      "dnf": "mtools",      "pacman": "mtools",      "brew": "mtools"},
    "rsync":     {"apt": "rsync",       "dnf": "rsync",       "pacman": "rsync",       "brew": "rsync"},
    "xorriso":   {"apt": "xorriso",     "dnf": "xorriso",     "pacman": "libisoburn",  "brew": "xorriso"},
    "genisoimage": {"apt": "genisoimage", "dnf": "genisoimage", "pacman": "cdrtools",  "brew": "cdrtools"},
    "sgdisk":      {"apt": "gdisk",       "dnf": "gdisk",       "pacman": "gptfdisk",  "brew": "gptfdisk"},
}


def _detect_pkg_manager() -> tuple[str, str]:
    """Detect the package manager. Returns (command, name)."""
    for cmd, name in [("apt-get", "apt"), ("dnf", "dnf"), ("yum", "dnf"),
                      ("pacman", "pacman"), ("brew", "brew")]:
        if _which(cmd):
            return cmd, name
    return "", ""


def _install_packages(cfg: Config, packages: list[str]) -> bool:
    """Attempt to install packages via the system package manager."""
    pkg_cmd, pkg_name = _detect_pkg_manager()
    if not pkg_cmd:
        return False

    # Deduplicate
    pkgs = sorted(set(packages))
    cfg.log(f"  Installing: {' '.join(pkgs)} (via {pkg_cmd})")

    if pkg_name == "pacman":
        cmd = [pkg_cmd, "-S", "--noconfirm"] + pkgs
    elif pkg_name == "brew":
        cmd = [pkg_cmd, "install"] + pkgs
    else:
        cmd = [pkg_cmd, "install", "-y"] + pkgs

    try:
        # brew runs as user, not root
        r = _run(cfg, cmd, check=False, verbose=True,
                 as_root=(pkg_name != "brew"))
        if r.returncode != 0:
            cfg.log(f"  Install failed: {r.stderr.strip()}")
            return False
        return True
    except Exception as e:
        cfg.log(f"  Install failed: {e}")
        return False


def _resolve_packages(tools: list[str]) -> list[str]:
    """Map missing tool names to installable package names."""
    _, pkg_name = _detect_pkg_manager()
    if not pkg_name:
        return tools
    pkgs = []
    for tool in tools:
        if tool in _TOOL_PACKAGES and pkg_name in _TOOL_PACKAGES[tool]:
            pkgs.append(_TOOL_PACKAGES[tool][pkg_name])
        else:
            pkgs.append(tool)
    return sorted(set(pkgs))


def check_tools_img() -> list[str]:
    """Check tools needed for FAT32 .img creation. Returns missing tools."""
    missing: list[str] = []
    for tool in ["dd", "mkfs.vfat", "mcopy"]:
        if not _which(tool):
            missing.append(tool)
    return missing


def check_tools_iso() -> list[str]:
    """Check tools needed for ISO creation. Returns missing tools."""
    if _which("xorriso"):
        return []
    if _which("genisoimage"):
        return []
    return ["xorriso"]


def check_tools_gpt() -> list[str]:
    """Check tools needed for GPT image creation. Returns missing tools."""
    missing: list[str] = []
    base_tools = ["dd", "mkfs.vfat", "rsync", "sgdisk"]
    # macOS uses hdiutil instead of losetup for loop devices
    if _is_macos():
        base_tools.append("hdiutil")
    else:
        base_tools.append("losetup")
    for tool in base_tools:
        if not _which(tool):
            missing.append(tool)
    return missing


def ensure_tools(cfg: Config, fmt: str) -> None:
    """Check for required tools and auto-install if missing.

    Raises RuntimeError if tools cannot be installed.
    """
    if fmt == "img":
        missing = check_tools_img()
    elif fmt == "gpt":
        missing = check_tools_gpt()
    else:
        missing = check_tools_iso()

    if not missing:
        return

    cfg.log(f"  Missing tools: {', '.join(missing)}")
    packages = _resolve_packages(missing)

    cfg.log(f"  Attempting auto-install...")
    if _install_packages(cfg, packages):
        # Verify installation worked
        if fmt == "img":
            still_missing = check_tools_img()
        elif fmt == "gpt":
            still_missing = check_tools_gpt()
        else:
            still_missing = check_tools_iso()
        if not still_missing:
            cfg.log(f"  Tools installed successfully.")
            return

    # Failed — give manual instructions
    pkg_cmd, _ = _detect_pkg_manager()
    if _is_windows():
        msg = f"Missing tools. Run in WSL:\n    sudo apt install {' '.join(packages)}"
    elif pkg_cmd:
        msg = f"Auto-install failed. Run manually:\n    sudo {pkg_cmd} install {' '.join(packages)}"
    else:
        msg = f"Missing tools: {', '.join(missing)}\n    Install packages: {' '.join(packages)}"
    raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

def collect_files(cfg: Config, source_dir: str,
                  includes: list[str]) -> dict[str, str]:
    """Collect files to include in the image.

    Returns a dict of {image_path: local_path}. image_path uses forward
    slashes and is relative to the image root.

    Raises FileNotFoundError if source_dir does not exist.
    """
    files: dict[str, str] = {}
    src = Path(source_dir)

    if not src.is_dir():
        raise FileNotFoundError(f"{source_dir} is not a directory")

    # Recursively add all files from source directory
    for p in sorted(src.rglob("*")):
        if p.is_file():
            rel = str(PurePosixPath(p.relative_to(src)))
            files[rel] = str(p.resolve())

    # Add extra include files
    for inc in includes:
        p = Path(inc)
        if not p.exists():
            cfg.log(f"Warning: --include {inc} not found, skipping")
            continue
        if p.is_file():
            files[p.name] = str(p.resolve())
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    rel = str(PurePosixPath(f.relative_to(p)))
                    files[rel] = str(f.resolve())

    return files


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _calculate_content_size(files: dict[str, str]) -> int:
    """Calculate total content size in MB (ceiling division, minimum 1)."""
    total_bytes = sum(os.path.getsize(lp) for lp in files.values())
    return max(1, -(-total_bytes // (1024 * 1024)))


def _stage_files(files: dict[str, str], staging_dir: Path) -> None:
    """Copy files to a staging directory, preserving relative paths."""
    for img_path, local_path in files.items():
        dest = staging_dir / img_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)


def _populate_partition(cfg: Config, staging_dir: str,
                        partition: str) -> None:
    """Mount a partition, rsync staged files into it, unmount.

    Raises RuntimeError on failure.
    """
    if cfg.verbose:
        rsync_flags = "-av --no-owner --no-group"
    else:
        rsync_flags = "-a --no-owner --no-group --info=progress2"

    r = _run(cfg, [
        "bash", "-c",
        f"MNTDIR=$(mktemp -d) && "
        f"mount '{partition}' $MNTDIR && "
        f"rsync {rsync_flags} '{staging_dir}'/ $MNTDIR/ && "
        f"sync && "
        f"umount $MNTDIR && "
        f"rmdir $MNTDIR"
    ], check=False, verbose=True, as_root=True)

    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to populate {partition}: {r.stderr.strip()}"
        )


def _parse_size(size_str: str) -> int:
    """Parse a human-readable size string to megabytes.

    Accepts: "4G", "4g", "512M", "512m", "1024" (plain MB).
    Returns 0 for empty string.
    Raises ValueError for invalid format.
    """
    if not size_str:
        return 0
    s = size_str.strip().upper()
    if s.endswith("G"):
        return int(s[:-1]) * 1024
    if s.endswith("M"):
        return int(s[:-1])
    return int(s)


def _setup_loop_device(cfg: Config, image_path: str) -> str:
    """Attach image to a loop/disk device with partition scanning.

    On Linux: uses losetup. Returns /dev/loopN.
    On macOS: uses hdiutil attach. Returns /dev/diskN.
    Raises RuntimeError on failure.
    """
    if _is_macos():
        r = _run(cfg, [
            "hdiutil", "attach", "-nomount",
            "-imagekey", "diskimage-class=CRawDiskImage",
            image_path,
        ], verbose=True, as_root=True)
        # hdiutil outputs lines like "/dev/disk4  GUID_partition_scheme"
        for line in r.stdout.strip().splitlines():
            parts = line.split()
            if parts and parts[0].startswith("/dev/disk"):
                dev = parts[0]
                # Return the base disk device (not partition)
                if "s" not in dev.split("disk")[-1]:
                    return dev
        # Fallback: first /dev/disk* token
        for line in r.stdout.strip().splitlines():
            for token in line.split():
                if token.startswith("/dev/disk"):
                    return token
        raise RuntimeError(f"hdiutil attach returned no device: {r.stdout}")

    r = _run(cfg, [
        "losetup", "--find", "--show", "--partscan", image_path
    ], verbose=True, as_root=True)
    loop_dev = r.stdout.strip()
    if not loop_dev:
        raise RuntimeError("losetup returned no device")
    return loop_dev


def _wait_for_partition(cfg: Config, loop_dev: str,
                        part_num: int, retries: int = 10) -> str:
    """Wait for a partition device to appear.

    On Linux: /dev/loopNpM. On macOS: /dev/diskNsM.
    Raises RuntimeError if it doesn't appear within retries.
    """
    import time
    if _is_macos():
        part_path = f"{loop_dev}s{part_num}"
    else:
        part_path = f"{loop_dev}p{part_num}"
    for _ in range(retries):
        r = _run(cfg, ["test", "-b", part_path], check=False, as_root=True)
        if r.returncode == 0:
            return part_path
        if not _is_macos():
            _run(cfg, ["partprobe", loop_dev], check=False, as_root=True)
        time.sleep(0.5)
    raise RuntimeError(
        f"Partition {part_path} did not appear after {retries} retries"
    )


def _teardown_loop_device(cfg: Config, loop_dev: str) -> None:
    """Detach a loop/disk device."""
    if _is_macos():
        _run(cfg, ["hdiutil", "detach", loop_dev], check=False, as_root=True)
    else:
        _run(cfg, ["losetup", "-d", loop_dev], check=False, as_root=True)


def _suggest_install(tool: str) -> str:
    """Return a platform-appropriate install command for a tool."""
    packages = _resolve_packages([tool])
    pkg_cmd, _ = _detect_pkg_manager()
    if pkg_cmd:
        return f"sudo {pkg_cmd} install {' '.join(packages)}"
    return f"Install package: {' '.join(packages)}"


def _check_root(cfg: Config, operation: str) -> None:
    """Check if we have root access. Raises RuntimeError if not.

    On Linux, checks euid. On Windows, assumes WSL has root via wsl -u root.
    """
    if _is_windows():
        return  # WSL uses wsl -u root, no check needed
    if os.geteuid() != 0:
        detail = "hdiutil + mount" if _is_macos() else "losetup + mount"
        raise RuntimeError(
            f"{operation} requires root ({detail}). Run with sudo."
        )


# ---------------------------------------------------------------------------
# Filesystem formatting (DRY — used by all builders)
# ---------------------------------------------------------------------------

def _format_partition(cfg: Config, device: str, fs_type: str,
                      label: str) -> None:
    """Format a partition with the specified filesystem.

    Args:
        fs_type: 'fat32', 'exfat', or 'ntfs'.
    """
    label = label[:11]  # FAT32/exFAT label limit
    if fs_type == "fat32":
        _run(cfg, [_find_tool("mkfs.vfat"), "-F", "32", "-n", label, device],
             verbose=True, as_root=True)
    elif fs_type == "exfat":
        _run(cfg, [_find_tool("mkfs.exfat"), "-n", label, device],
             verbose=True, as_root=True)
    elif fs_type == "ntfs":
        _run(cfg, [_find_tool("mkfs.ntfs"), "-f", "-L", label, device],
             verbose=True, as_root=True)
    else:
        raise ValueError(f"Unknown filesystem type: {fs_type}")


def check_tools_fs(fs_type: str) -> list[str]:
    """Check tools needed for a specific filesystem. Returns missing tools."""
    tools: dict[str, str] = {
        "fat32": "mkfs.vfat",
        "exfat": "mkfs.exfat",
        "ntfs": "mkfs.ntfs",
    }
    tool = tools.get(fs_type)
    if tool and not _which(tool):
        return [tool]
    return []


# ---------------------------------------------------------------------------
# Write verification
# ---------------------------------------------------------------------------

def _verify_write(cfg: Config, source_files: dict[str, str],
                  image_path: str) -> bool:
    """Verify written image by comparing file hashes.

    Uses mcopy to extract files from the image and compares SHA256 hashes
    to the source files. No root needed.

    Returns True if all files match, False if any mismatch.
    """
    import hashlib

    def _sha256(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    if not _which("mcopy"):
        cfg.log("  Warning: mcopy not available, skipping verification")
        return True

    cfg.log(f"  Verifying {len(source_files)} files...")
    failures = 0
    for img_path, local_path in sorted(source_files.items()):
        src_hash = _sha256(local_path)
        r = _run(cfg, ["mcopy", "-i", image_path, f"::{img_path}", "-"],
                 check=False)
        if r.returncode != 0:
            cfg.log(f"  VERIFY FAIL: {img_path} (extract failed)")
            failures += 1
            continue
        # mcopy outputs to stdout as text, but we need binary comparison
        # Re-extract with subprocess directly for binary data
        import subprocess as _sp
        rr = _sp.run(["mcopy", "-i", image_path, f"::{img_path}", "-"],
                     capture_output=True)
        img_hash = _sha256_bytes(rr.stdout)
        if src_hash != img_hash:
            cfg.log(f"  VERIFY FAIL: {img_path} (hash mismatch)")
            failures += 1
        elif cfg.verbose:
            cfg.log(f"  VERIFY OK: {img_path}")

    if failures == 0:
        cfg.log(f"  Verification passed: all {len(source_files)} files match")
        return True
    cfg.log(f"  Verification FAILED: {failures} file(s) differ")
    return False


# ---------------------------------------------------------------------------
# Image compression
# ---------------------------------------------------------------------------

def _is_compressed_path(path: str) -> bool:
    """Check if path has a compression extension."""
    return any(path.endswith(ext) for ext in (".gz", ".zst", ".xz"))


def _strip_compression_ext(path: str) -> str:
    """Strip compression extension: 'foo.img.gz' -> 'foo.img'."""
    for ext in (".gz", ".zst", ".xz"):
        if path.endswith(ext):
            return path[:-len(ext)]
    return path


def _compress_file(cfg: Config, input_path: str, output_path: str) -> None:
    """Compress a file. Detects format from output extension."""
    import gzip as _gzip
    import lzma as _lzma

    cfg.log(f"  Compressing to {Path(output_path).name}...")
    if output_path.endswith(".gz"):
        with open(input_path, "rb") as fin, _gzip.open(output_path, "wb") as fout:
            while True:
                chunk = fin.read(1024 * 1024)
                if not chunk:
                    break
                fout.write(chunk)
    elif output_path.endswith(".xz"):
        with open(input_path, "rb") as fin, _lzma.open(output_path, "wb") as fout:
            while True:
                chunk = fin.read(1024 * 1024)
                if not chunk:
                    break
                fout.write(chunk)
    elif output_path.endswith(".zst"):
        if not _which("zstd"):
            raise RuntimeError("zstd not found. Install zstd or use .gz/.xz")
        _run(cfg, ["zstd", "-f", "-o", output_path, input_path], verbose=True)
    else:
        raise ValueError(f"Unknown compression format: {output_path}")

    in_size = os.path.getsize(input_path)
    out_size = os.path.getsize(output_path)
    ratio = out_size / in_size * 100 if in_size > 0 else 0
    cfg.log(f"  Compressed: {in_size // 1024}KB -> {out_size // 1024}KB ({ratio:.0f}%)")


def _decompress_pipe_cmd(source_path: str) -> list[str]:
    """Return decompression command for piping to dd."""
    if source_path.endswith(".gz"):
        return ["gzip", "-dc", source_path]
    if source_path.endswith(".xz"):
        return ["xz", "-dc", source_path]
    if source_path.endswith(".zst"):
        return ["zstd", "-dc", source_path]
    return ["cat", source_path]


# ---------------------------------------------------------------------------
# Image modify (add/remove files from existing FAT32 image)
# ---------------------------------------------------------------------------

def modify_img(cfg: Config, image: str, add_paths: list[str],
               remove_paths: list[str]) -> None:
    """Add or remove files from an existing FAT32 image using mtools.

    No root needed. FAT32 only (mtools limitation).
    """
    if not _which("mcopy"):
        raise RuntimeError("mcopy not found. Install mtools.")

    img = _resolve(image)

    # Remove files first
    for path in remove_paths:
        cfg.log(f"  Removing ::{path}")
        _run(cfg, ["mdel", "-i", img, f"::{path}"], check=False,
             verbose=cfg.verbose)

    # Add files
    for path in add_paths:
        p = Path(path)
        if not p.exists():
            cfg.log(f"  Warning: {path} not found, skipping")
            continue
        if p.is_file():
            cfg.log(f"  Adding {p.name}")
            _run(cfg, ["mcopy", "-i", img, "-o", str(p.resolve()), f"::{p.name}"],
                 verbose=cfg.verbose)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    rel = str(PurePosixPath(f.relative_to(p)))
                    # Create directories
                    parts = PurePosixPath(rel).parts
                    for i in range(1, len(parts)):
                        d = str(PurePosixPath(*parts[:i]))
                        _run(cfg, ["mmd", "-i", img, f"::{d}"], check=False)
                    cfg.log(f"  Adding {rel}")
                    _run(cfg, ["mcopy", "-i", img, "-o", str(f.resolve()),
                               f"::{rel}"], verbose=cfg.verbose)

    cfg.log(f"  [OK] Modified {image}")


# ---------------------------------------------------------------------------
# FAT32 .img builder
# ---------------------------------------------------------------------------

def _populate_img_mcopy(cfg: Config, files: dict[str, str],
                        out: str) -> None:
    """Populate a FAT32 image using mcopy/mmd (no root needed)."""
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


def _populate_img_mount(cfg: Config, staging_dir: str, out: str,
                        size_mb: int) -> bool:
    """Populate a FAT32 image using mount+rsync (requires root).

    Returns True on success, False on failure.
    """
    if cfg.verbose:
        rsync_flags = "-av --no-owner --no-group"
    else:
        rsync_flags = "-a --no-owner --no-group --info=progress2"

    if _is_windows():
        r = _run(cfg, [
            "bash", "-c",
            f"set -e && "
            f"IMG=$(mktemp /tmp/mkimage.XXXXXX.img) && "
            f"dd if=/dev/zero of=$IMG bs=1M count={size_mb} 2>/dev/null && "
            f"mkfs.vfat -F 32 -n '{cfg.label[:11]}' $IMG >/dev/null && "
            f"MNTDIR=$(mktemp -d) && "
            f"mount -o loop $IMG $MNTDIR && "
            f"rsync {rsync_flags} '{staging_dir}'/ $MNTDIR/ && "
            f"TOTAL=$(find $MNTDIR -type f | wc -l) && "
            f"echo \"Copied $TOTAL files\" && "
            f"umount $MNTDIR && "
            f"rmdir $MNTDIR && "
            f"cp $IMG '{out}' && "
            f"rm -f $IMG"
        ], check=False, verbose=True, as_root=True)
    else:
        dd_cmd = ["dd", "if=/dev/zero", f"of={out}", "bs=1M", f"count={size_mb}"]
        if cfg.verbose:
            dd_cmd.append("status=progress")
        _run(cfg, dd_cmd, verbose=True)
        _run(cfg, ["mkfs.vfat", "-F", "32", "-n", cfg.label[:11], out], verbose=True)

        r = _run(cfg, [
            "bash", "-c",
            f"MNTDIR=$(mktemp -d) && "
            f"mount -o loop '{out}' $MNTDIR && "
            f"rsync {rsync_flags} '{staging_dir}'/ $MNTDIR/ && "
            f"TOTAL=$(find $MNTDIR -type f | wc -l) && "
            f"echo \"Copied $TOTAL files\" && "
            f"umount $MNTDIR && "
            f"rmdir $MNTDIR"
        ], check=False, verbose=True, as_root=True)

    return r.returncode == 0


def build_img(cfg: Config, files: dict[str, str], output: str) -> None:
    """Create a FAT32 disk image (no partition table).

    Primary method uses mcopy (no root needed). Falls back to mount+rsync
    if mcopy is unavailable and root access is available.
    """
    ensure_tools(cfg, "img")
    out = _resolve(output)

    content_mb = _calculate_content_size(files)
    size_mb = max(40, content_mb + cfg.extra_mb)
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
            _run(cfg, ["mkfs.vfat", "-F", "32", "-n", cfg.label[:11], out],
                 verbose=True)
            _populate_img_mcopy(cfg, files, out)
        elif _which("rsync"):
            # Fallback: mount+rsync (requires root)
            cfg.log("  mcopy not found, using mount+rsync (requires root)...")
            cfg.log(f"  Copying {len(files)} files to image...")
            if not _populate_img_mount(cfg, stg_resolved, out, size_mb):
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


# ---------------------------------------------------------------------------
# ISO builder
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# GPT image builders
# ---------------------------------------------------------------------------

def build_gpt_img(cfg: Config, files: dict[str, str], output: str) -> None:
    """Create a GPT disk image with a single EFI System Partition."""
    _check_root(cfg, "GPT image creation")
    ensure_tools(cfg, "gpt")
    out = _resolve(output)

    content_mb = _calculate_content_size(files)
    esp_mb = max(int(content_mb * 1.3 + 10), 64)
    total_mb = esp_mb + 2  # 2MB GPT overhead
    esp_label = cfg.esp_label[:11]
    cfg.log(f"  GPT image: {total_mb}MB total ({esp_mb}MB ESP, "
            f"{content_mb}MB content)")

    with tempfile.TemporaryDirectory() as staging:
        _stage_files(files, Path(staging))
        stg_resolved = _resolve(staging)

        # Create sparse image
        _run(cfg, ["dd", "if=/dev/zero", f"of={out}", "bs=1M",
                   "count=0", f"seek={total_mb}"], verbose=cfg.verbose)

        # GPT partition table
        _run(cfg, ["sgdisk", "-Z", out], verbose=cfg.verbose)
        _run(cfg, ["sgdisk", "-o", out], verbose=cfg.verbose)
        _run(cfg, ["sgdisk",
                   "-n", f"1:2048:+{esp_mb}M",
                   "-t", "1:EF00",
                   "-c", f"1:{esp_label}",
                   out], verbose=True)

        loop_dev = _setup_loop_device(cfg, out)
        try:
            esp_part = _wait_for_partition(cfg, loop_dev, 1)

            cfg.log(f"  Formatting ESP ({esp_label})...")
            _format_partition(cfg, esp_part, cfg.fs_type, esp_label)

            cfg.log(f"  Copying {len(files)} files to ESP...")
            _populate_partition(cfg, stg_resolved, esp_part)
        finally:
            _teardown_loop_device(cfg, loop_dev)

    actual_size = os.path.getsize(output)
    cfg.log(f"  [OK] Created {output} ({actual_size // (1024*1024)}MB, "
            f"GPT+ESP)")

    if cfg.verify:
        _verify_write(cfg, files, output)


def build_gpt_data_img(cfg: Config, esp_files: dict[str, str],
                       data_files: dict[str, str], output: str) -> None:
    """Create a GPT disk image with ESP + data partition."""
    _check_root(cfg, "GPT image creation")
    ensure_tools(cfg, "gpt")
    out = _resolve(output)

    esp_content_mb = _calculate_content_size(esp_files)
    esp_mb = max(int(esp_content_mb * 1.3 + 10), 64)

    data_content_mb = _calculate_content_size(data_files) if data_files else 1
    data_mb = (_parse_size(cfg.data_size) if cfg.data_size
               else max(int(data_content_mb * 1.3 + 10), 34))

    total_mb = esp_mb + data_mb + 2  # 2MB GPT overhead
    esp_label = cfg.esp_label[:11]
    data_label = cfg.data_label[:11]
    cfg.log(f"  GPT image: {total_mb}MB total "
            f"({esp_mb}MB ESP + {data_mb}MB data, "
            f"{esp_content_mb}MB + {data_content_mb}MB content)")

    with tempfile.TemporaryDirectory() as staging:
        esp_staging = Path(staging) / "esp"
        data_staging = Path(staging) / "data"
        esp_staging.mkdir()
        _stage_files(esp_files, esp_staging)
        data_staging.mkdir()
        if data_files:
            _stage_files(data_files, data_staging)

        esp_stg = _resolve(str(esp_staging))
        data_stg = _resolve(str(data_staging))

        # Create sparse image
        _run(cfg, ["dd", "if=/dev/zero", f"of={out}", "bs=1M",
                   "count=0", f"seek={total_mb}"], verbose=cfg.verbose)

        # GPT partition table with two partitions
        _run(cfg, ["sgdisk", "-Z", out], verbose=cfg.verbose)
        _run(cfg, ["sgdisk", "-o", out], verbose=cfg.verbose)
        _run(cfg, ["sgdisk",
                   "-n", f"1:2048:+{esp_mb}M",
                   "-t", "1:EF00",
                   "-c", f"1:{esp_label}",
                   out], verbose=True)
        _run(cfg, ["sgdisk",
                   "-n", "2:0:0",
                   "-t", "2:0700",
                   "-c", f"2:{data_label}",
                   out], verbose=True)

        loop_dev = _setup_loop_device(cfg, out)
        try:
            esp_part = _wait_for_partition(cfg, loop_dev, 1)
            data_part = _wait_for_partition(cfg, loop_dev, 2)

            # Format and populate ESP
            cfg.log(f"  Formatting ESP ({esp_label})...")
            _format_partition(cfg, esp_part, cfg.fs_type, esp_label)
            cfg.log(f"  Copying {len(esp_files)} files to ESP...")
            _populate_partition(cfg, esp_stg, esp_part)

            # Format and populate data
            cfg.log(f"  Formatting data ({data_label})...")
            _format_partition(cfg, data_part, cfg.fs_type, data_label)
            if data_files:
                cfg.log(f"  Copying {len(data_files)} files to data...")
                _populate_partition(cfg, data_stg, data_part)
        finally:
            _teardown_loop_device(cfg, loop_dev)

    actual_size = os.path.getsize(output)
    cfg.log(f"  [OK] Created {output} ({actual_size // (1024*1024)}MB, "
            f"GPT+ESP+DATA)")


# ---------------------------------------------------------------------------
# USB write
# ---------------------------------------------------------------------------

MAX_USB_SIZE_GB = 2048


def _list_removable_drives_linux() -> list[dict[str, str]]:
    """List removable drives on Linux via lsblk."""
    drives: list[dict[str, str]] = []
    quiet = Config()
    r = _run(quiet, ["lsblk", "-d", "-n", "-o", "NAME,SIZE,RM,TYPE,MODEL,TRAN",
              "--bytes"], check=False)
    if r.returncode != 0:
        return drives

    for line in r.stdout.strip().splitlines():
        parts = line.split(None, 5)
        if len(parts) < 4:
            continue
        name, size_bytes_str, removable, dtype = parts[0], parts[1], parts[2], parts[3]
        model = parts[4] if len(parts) > 4 else ""
        transport = parts[5].strip() if len(parts) > 5 else ""

        if dtype != "disk":
            continue
        if removable != "1" and "usb" not in transport.lower():
            continue

        dev_path = f"/dev/{name}"

        mr = _run(quiet, ["lsblk", "-n", "-o", "MOUNTPOINT", dev_path], check=False)
        mountpoints = mr.stdout.strip().splitlines() if mr.returncode == 0 else []
        if any(mp.strip() == "/" for mp in mountpoints):
            continue

        try:
            size_bytes = int(size_bytes_str)
        except ValueError:
            continue

        size_gb = size_bytes / (1024 ** 3)
        if size_gb > MAX_USB_SIZE_GB:
            continue

        size_str = f"{size_gb:.1f}GB" if size_gb >= 1 else f"{size_bytes // (1024 * 1024)}MB"
        drives.append({
            "name": name, "path": dev_path, "size": size_str,
            "size_bytes": str(size_bytes), "model": model.strip(),
        })
    return drives


def _list_removable_drives_windows() -> list[dict[str, str]]:
    """List removable USB drives on Windows via PowerShell Get-Disk."""
    drives: list[dict[str, str]] = []
    ps_cmd = (
        "Get-Disk | Where-Object { $_.BusType -eq 'USB' } | "
        "ForEach-Object { "
        "  $num = $_.Number; $size = $_.Size; $model = $_.FriendlyName; "
        "  $hasC = (Get-Partition -DiskNumber $num -ErrorAction SilentlyContinue | "
        "    Where-Object { $_.DriveLetter -eq 'C' }).Count -gt 0; "
        "  if (-not $hasC) { "
        "    Write-Output \"$num|$size|$model\" "
        "  } "
        "}"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            return drives

        for line in r.stdout.strip().splitlines():
            parts = line.strip().split("|", 2)
            if len(parts) < 3:
                continue
            num_str, size_str, model = parts
            try:
                disk_num = int(num_str)
                size_bytes = int(size_str)
            except ValueError:
                continue

            size_gb = size_bytes / (1024 ** 3)
            if size_gb > MAX_USB_SIZE_GB:
                continue

            size_display = f"{size_gb:.1f}GB" if size_gb >= 1 else f"{size_bytes // (1024 * 1024)}MB"
            drives.append({
                "name": f"disk{disk_num}",
                "path": f"\\\\.\\PhysicalDrive{disk_num}",
                "size": size_display,
                "size_bytes": str(size_bytes),
                "model": model.strip(),
            })
    except FileNotFoundError:
        pass
    return drives


def _list_removable_drives_macos() -> list[dict[str, str]]:
    """List removable USB drives on macOS via diskutil."""
    drives: list[dict[str, str]] = []
    quiet = Config()
    # Get external/removable disks
    r = _run(quiet, ["diskutil", "list", "external"], check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return drives

    # Parse diskutil list output for /dev/diskN lines
    import re
    for line in r.stdout.splitlines():
        m = re.match(r"^(/dev/disk\d+)\s+\(external", line)
        if not m:
            continue
        dev_path = m.group(1)

        # Get details via diskutil info
        info_r = _run(quiet, ["diskutil", "info", dev_path], check=False)
        if info_r.returncode != 0:
            continue

        info = info_r.stdout
        size_bytes = 0
        model = ""
        for info_line in info.splitlines():
            info_line = info_line.strip()
            if info_line.startswith("Disk Size:"):
                # "Disk Size:  15728640000 Bytes ..."
                size_match = re.search(r"(\d+)\s+Bytes", info_line)
                if size_match:
                    size_bytes = int(size_match.group(1))
            elif info_line.startswith("Device / Media Name:"):
                model = info_line.split(":", 1)[1].strip()

        if size_bytes == 0:
            continue

        size_gb = size_bytes / (1024 ** 3)
        if size_gb > MAX_USB_SIZE_GB:
            continue

        name = dev_path.replace("/dev/", "")
        size_str = f"{size_gb:.1f}GB" if size_gb >= 1 else f"{size_bytes // (1024 * 1024)}MB"
        drives.append({
            "name": name, "path": dev_path, "size": size_str,
            "size_bytes": str(size_bytes), "model": model,
        })

    return drives


def _list_removable_drives() -> list[dict[str, str]]:
    """List removable USB drives. Auto-detects platform."""
    if _is_windows():
        return _list_removable_drives_windows()
    if _is_macos():
        return _list_removable_drives_macos()
    return _list_removable_drives_linux()


def _verify_usb_bus(cfg: Config, device: str) -> bool:
    """Verify a device is on the USB bus.

    Uses udevadm on Linux, diskutil on macOS.
    Returns True if USB (or if verification is unavailable).
    Returns False if the device is on a non-USB bus (SATA, NVMe, etc.).
    """
    if _is_windows():
        return True  # Windows checks bus type via Get-Disk

    if _is_macos():
        r = _run(cfg, ["diskutil", "info", device], check=False)
        if r.returncode != 0:
            return True
        for line in r.stdout.splitlines():
            if "Protocol:" in line:
                return "USB" in line
        return True  # can't determine protocol, allow

    # Linux: udevadm
    if not _which("udevadm"):
        cfg.log("  Warning: udevadm not found, skipping bus verification")
        return True
    r = _run(cfg, ["udevadm", "info", "--query=property", f"--name={device}"],
             check=False)
    if r.returncode != 0:
        return True  # can't determine, allow
    return "ID_BUS=usb" in r.stdout


def _unmount_device(cfg: Config, device: str) -> None:
    """Unmount all mounted partitions on a device."""
    cfg.log(f"  Unmounting {device} partitions...")

    if _is_macos():
        _run(cfg, ["diskutil", "unmountDisk", device], check=False)
        return

    if _which("findmnt"):
        r = _run(cfg, ["findmnt", "--list", "--noheadings", "-o", "TARGET",
                       "--source", device], check=False)
        # Also check partitions
        for suffix in [str(i) for i in range(1, 10)] + [f"p{i}" for i in range(1, 10)]:
            rp = _run(cfg, ["findmnt", "--list", "--noheadings", "-o", "TARGET",
                            "--source", f"{device}{suffix}"], check=False)
            if rp.stdout.strip():
                for mnt in rp.stdout.strip().splitlines():
                    _run(cfg, ["umount", mnt.strip()], check=False, as_root=True)
        if r.stdout.strip():
            for mnt in r.stdout.strip().splitlines():
                _run(cfg, ["umount", mnt.strip()], check=False, as_root=True)
    else:
        # Fallback: try unmounting common partition patterns
        _run(cfg, ["umount", device], check=False, as_root=True)
        for i in range(1, 10):
            _run(cfg, ["umount", f"{device}{i}"], check=False, as_root=True)
            _run(cfg, ["umount", f"{device}p{i}"], check=False, as_root=True)


def _resolve_usb_target(cfg: Config, target: str,
                        select_drive: Optional[Callable[..., Optional[dict[str, str]]]] = None,
                        ) -> Optional[dict[str, str]]:
    """Resolve a USB target to a drive dict.

    If target is 'usb', auto-detect. If target is /dev/sdX, find it in
    the drive list. Returns drive dict or None if aborted.
    """
    if select_drive is None:
        select_drive = _cli_select_drive

    drives = _list_removable_drives()

    if target.lower() == "usb":
        if not drives:
            cfg.log("No removable USB drives found.")
            cfg.log("  - On Windows/WSL, USB passthrough may need usbipd")
            cfg.log(f"  - Drive must be removable and under {MAX_USB_SIZE_GB}GB")
            return None
        if len(drives) == 1:
            cfg.log(f"  Auto-detected: {drives[0]['path']} "
                    f"({drives[0]['size']} {drives[0]['model']})")
            return drives[0]
        cfg.log(f"Found {len(drives)} removable drives")
        return select_drive(drives)

    # Explicit device path — find it in drives list or create entry
    for d in drives:
        if d["path"] == target:
            return d

    # Device not in removable list — might be valid but not detected as removable
    cfg.log(f"Warning: {target} not found in removable drives list")
    return {"name": Path(target).name, "path": target, "size": "?",
            "size_bytes": "0", "model": ""}


def _usb_safety_checks(cfg: Config, drive: dict[str, str]) -> bool:
    """Run USB safety checks. Returns True if safe to proceed."""
    device = drive["path"]
    size_bytes = int(drive["size_bytes"])

    # Bus verification
    if not _verify_usb_bus(cfg, device):
        cfg.log(f"Error: {device} is not on the USB bus. Refusing to write.")
        cfg.log("  Use a USB-connected drive, not SATA/NVMe.")
        return False

    # System partition check
    if not _is_windows() and "sda" in drive["name"]:
        mr = _run(cfg, ["lsblk", "-n", "-o", "MOUNTPOINT", device], check=False)
        mounts = mr.stdout.strip() if mr.returncode == 0 else ""
        if "/" in mounts or "/boot" in mounts or "/home" in mounts:
            cfg.log(f"Error: {device} has system partitions mounted. Refusing.")
            return False

    # Size limit
    if size_bytes > MAX_USB_SIZE_GB * (1024 ** 3):
        cfg.log(f"Error: {device} is larger than {MAX_USB_SIZE_GB}GB. Refusing.")
        return False

    return True


def _detect_source_type(source: str) -> str:
    """Detect source type: 'directory' or 'image'.

    Raises ValueError for unrecognized source.
    """
    p = Path(source)
    if p.is_dir():
        return "directory"
    if p.is_file() and p.suffix.lower() in (".img", ".iso"):
        return "image"
    if p.is_file():
        return "image"  # treat any file as image for dd
    raise ValueError(f"Source '{source}' is not a directory or file")


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


def _cli_select_drive(drives: list[dict[str, str]]) -> Optional[dict[str, str]]:
    """CLI drive selection via input()."""
    print(f"\nAvailable USB drives (removable, <={MAX_USB_SIZE_GB}GB):\n")
    for i, d in enumerate(drives):
        model = f"  {d['model']}" if d['model'] else ""
        print(f"  [{i + 1}] {d['path']}  {d['size']}{model}")
    print()
    try:
        choice = input("Select drive number (or 'q' to quit): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if choice.lower() in ("q", "quit", ""):
        return None
    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(drives):
            raise ValueError()
    except ValueError:
        print(f"Error: invalid selection '{choice}'", file=sys.stderr)
        return None
    return drives[idx]


def _cli_confirm_write(target: dict[str, str]) -> bool:
    """CLI write confirmation via input()."""
    print(f"\n  WARNING: ALL DATA on {target['path']} "
          f"({target['size']} {target['model']}) WILL BE DESTROYED.\n")
    try:
        confirm = input(f"  Type 'yes' to write to {target['path']}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    return confirm == "yes"


def write_usb(
    cfg: Config,
    image_path: str,
    source_dir: str = "",
    includes: Optional[list[str]] = None,
    select_drive: Optional[Callable[..., Optional[dict[str, str]]]] = None,
    confirm_write: Optional[Callable[..., bool]] = None,
) -> None:
    """Write files to a USB drive with safety checks.

    On Windows: uses diskpart to format USB as FAT32, then copies files directly.
    On Linux: writes a pre-built .img file via dd.

    Args:
        cfg: Runtime configuration.
        image_path: Path to .img file (used on Linux for dd, or as fallback on Windows).
        source_dir: Source directory containing files to copy (Windows diskpart path).
        includes: Extra files/directories to include (Windows diskpart path).
        select_drive: Callback(drives_list) -> drive_dict or None. Defaults to CLI input().
        confirm_write: Callback(target_dict) -> bool. Defaults to CLI input().
    """
    if select_drive is None:
        select_drive = _cli_select_drive
    if confirm_write is None:
        confirm_write = _cli_confirm_write

    cfg.log("Scanning for removable drives...")
    drives = _list_removable_drives()

    if not drives:
        cfg.log("No removable USB drives found.")
        cfg.log("  - On Windows/WSL, USB passthrough may need usbipd")
        cfg.log(f"  - Drive must be removable and under {MAX_USB_SIZE_GB}GB")
        return

    cfg.log(f"Found {len(drives)} removable drive(s)")
    target = select_drive(drives)
    if target is None:
        cfg.log("Aborted.")
        return

    target_path = target["path"]
    target_size = int(target["size_bytes"])

    # Safety checks
    if "sda" in target["name"] and not _is_windows():
        mr = _run(cfg, ["lsblk", "-n", "-o", "MOUNTPOINT", target_path], check=False)
        mounts = mr.stdout.strip() if mr.returncode == 0 else ""
        if "/" in mounts or "/boot" in mounts or "/home" in mounts:
            cfg.log(f"Error: {target_path} has system partitions mounted. Refusing.")
            return

    if target_size > MAX_USB_SIZE_GB * (1024 ** 3):
        cfg.log(f"Error: {target_path} is larger than {MAX_USB_SIZE_GB}GB. Refusing.")
        return

    if not confirm_write(target):
        cfg.log("Aborted.")
        return

    if _is_windows():
        _write_usb_windows(cfg, source_dir, includes or [], target)
    else:
        if not os.path.isfile(image_path):
            cfg.log(f"Error: {image_path} not found")
            return
        img_resolved = _resolve(image_path)
        img_size = os.path.getsize(image_path)
        _write_usb_linux(cfg, image_path, img_size, img_resolved, target)


def _write_usb_windows(
    cfg: Config, source_dir: str, includes: list[str],
    target: dict[str, str],
) -> None:
    """Format USB as FAT32 via diskpart and copy files directly.

    Uses an elevated PowerShell subprocess for the diskpart + copy.
    No raw disk write — just format and file copy.
    """
    target_path = target["path"]  # \\.\PhysicalDriveN
    disk_num = target_path.rsplit("PhysicalDrive", 1)[-1]
    label_trim = cfg.label[:11]

    cfg.log(f"Formatting disk {disk_num} as FAT32 and copying files...")

    fd, progress_file = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    fd, script_file = tempfile.mkstemp(suffix=".ps1")
    os.close(fd)

    prg_esc = progress_file.replace("'", "''")

    # Build source paths list for the elevated script
    sources: list[str] = []
    if source_dir and os.path.isdir(source_dir):
        sources.append(source_dir)
    for inc in includes:
        if inc and os.path.exists(inc):
            sources.append(inc)
    sources_str = ",".join(f"'{s.replace(chr(39), chr(39)*2)}'" for s in sources)
    verbose_str = "$true" if cfg.verbose else "$false"
    verify_str = "$true" if cfg.verify else "$false"
    gpt_str = "$true" if cfg.gpt else "$false"

    ps_script = f"""
try {{
    "Preparing USB drive (disk {disk_num})... [native Windows, no WSL]" | Out-File -Append '{prg_esc}'

    $useGpt = {gpt_str}
    $partStyle = if ($useGpt) {{ "GPT" }} else {{ "MBR" }}

    # Step 1: Take disk offline
    "  Taking disk offline..." | Out-File -Append '{prg_esc}'
    Set-Disk -Number {disk_num} -IsOffline $true -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1

    # Step 2: clean (separate session)
    "  diskpart: clean..." | Out-File -Append '{prg_esc}'
    ("select disk {disk_num}`r`nclean" | diskpart 2>&1) | Out-Null
    Start-Sleep -Seconds 1

    # Step 3: convert (separate session, ignore failure if already correct type)
    "  diskpart: convert $partStyle..." | Out-File -Append '{prg_esc}'
    ("select disk {disk_num}`r`nconvert $($partStyle.ToLower())" | diskpart 2>&1) | Out-Null
    Start-Sleep -Seconds 1

    # Step 4: create + format (separate session)
    if ($useGpt) {{
        $dpCreate = @"
select disk {disk_num}
create partition primary
select partition 1
format fs=fat32 quick label={label_trim}
"@
    }} else {{
        $dpCreate = @"
select disk {disk_num}
create partition primary
active
format fs=fat32 quick label={label_trim}
"@
    }}
    "  diskpart: create + format..." | Out-File -Append '{prg_esc}'
    $dpOut = ($dpCreate | diskpart 2>&1) | Out-String
    $dpSummary = $dpOut.Trim() -replace '[\r\n]+', ' | '
    "  diskpart: $dpSummary" | Out-File -Append '{prg_esc}'
    if ($dpOut -notmatch "successfully formatted") {{
        throw "diskpart failed. Output: $dpSummary"
    }}

    # Step 5: Disable automount, bring online
    "  Disabling automount, bringing disk online..." | Out-File -Append '{prg_esc}'
    "automount disable" | diskpart 2>&1 | Out-Null
    Set-Disk -Number {disk_num} -IsOffline $false -ErrorAction SilentlyContinue
    Set-Disk -Number {disk_num} -IsReadOnly $false -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2

    # Step 6: Get or assign drive letter
    $part = Get-Partition -DiskNumber {disk_num} -ErrorAction SilentlyContinue |
        Where-Object {{ $_.Type -ne 'Reserved' -and $_.Type -ne 'System' }} |
        Select-Object -First 1
    if (-not $part) {{ throw "No partition found after diskpart" }}

    $driveLetter = $part.DriveLetter
    if ($driveLetter -and $driveLetter -ne "`0") {{
        "  Partition already has drive letter ${{driveLetter}}:" | Out-File -Append '{prg_esc}'
    }} else {{
        $used = @((Get-CimInstance Win32_LogicalDisk).DeviceID -replace ':', '')
        $driveLetter = (69..90 | ForEach-Object {{ [char]$_ }} |
            Where-Object {{ "$_" -notin $used }} | Select-Object -First 1)
        if (-not $driveLetter) {{ throw "No free drive letters" }}
        "  Add-PartitionAccessPath ${{driveLetter}}:\" | Out-File -Append '{prg_esc}'
        $part | Add-PartitionAccessPath -AccessPath "${{driveLetter}}:\" -ErrorAction Stop
    }}
    Start-Sleep -Seconds 3

    $destRoot = "${{driveLetter}}:\"
    if (Test-Path $destRoot) {{
        "  Drive ${{driveLetter}}: accessible (FAT32)" | Out-File -Append '{prg_esc}'
    }} else {{
        throw "Drive ${{driveLetter}}: not accessible after mount"
    }}

    $sources = @({sources_str})
    $verbose = {verbose_str}
    $totalFiles = 0
    foreach ($s in $sources) {{
        if (-not $s) {{ continue }}
        if (Test-Path $s -PathType Leaf) {{ $totalFiles++ }}
        elseif (Test-Path $s -PathType Container) {{
            $totalFiles += (Get-ChildItem -LiteralPath $s -Recurse -File).Count
        }}
    }}
    "Copying $totalFiles files from $($sources.Count) source path(s) to $destRoot [robocopy]..." | Out-File -Append '{prg_esc}'
    $totalCopied = 0
    $totalFailed = 0

    foreach ($src in $sources) {{
        if (-not $src) {{ continue }}
        if (Test-Path $src -PathType Leaf) {{
            $fileName = Split-Path $src -Leaf
            $srcDir = Split-Path $src -Parent
            "  Copying file: $fileName" | Out-File -Append '{prg_esc}'
            robocopy $srcDir $destRoot $fileName /NJH /NJS /NP 2>&1 | Out-Null
            $rcExit = $LASTEXITCODE
            if ($rcExit -lt 8) {{ $totalCopied++ }} else {{
                "  WARN: robocopy exit $rcExit for $fileName" | Out-File -Append '{prg_esc}'
                $totalFailed++
            }}
        }} elseif (Test-Path $src -PathType Container) {{
            $srcNorm = $src.TrimEnd('\', '/')
            "Copying directory: $srcNorm -> $destRoot" | Out-File -Append '{prg_esc}'
            if ($verbose) {{
                $rcOut = robocopy $srcNorm $destRoot /S /E /NP /NJH /NJS 2>&1
            }} else {{
                $rcOut = robocopy $srcNorm $destRoot /S /E /NP /NFL /NDL /NJH /NJS 2>&1
            }}
            $rcExit = $LASTEXITCODE
            $rcText = ($rcOut | Out-String).Trim()
            if ($verbose -and $rcText) {{
                "ROBOCOPY output:`r`n$rcText" | Out-File -Append '{prg_esc}'
            }}
            if ($rcExit -lt 8) {{
                $fileCount = (Get-ChildItem -LiteralPath $srcNorm -Recurse -File).Count
                $totalCopied += $fileCount
                "Copied $fileCount files (robocopy exit $rcExit)" | Out-File -Append '{prg_esc}'
            }} else {{
                "robocopy FAILED for $srcNorm (exit $rcExit)`r`nOutput: $rcText" | Out-File -Append '{prg_esc}'
                $totalFailed++
            }}
        }}
    }}

    if (Test-Path "${{driveLetter}}:\") {{
        $fileCount = (Get-ChildItem "${{driveLetter}}:\" -Recurse -File -ErrorAction SilentlyContinue).Count
        "  Drive ${{driveLetter}}: accessible, $fileCount files on disk" | Out-File -Append '{prg_esc}'
    }} else {{
        "  WARNING: Drive ${{driveLetter}}: not accessible" | Out-File -Append '{prg_esc}'
    }}

    # Verify files if requested
    $verify = {verify_str}
    $verifyFailed = 0
    if ($verify -and $totalCopied -gt 0) {{
        "Verifying $totalCopied files [Get-FileHash MD5]..." | Out-File -Append '{prg_esc}'
        foreach ($src in $sources) {{
            if (-not $src) {{ continue }}
            if (Test-Path $src -PathType Leaf) {{
                $fileName = Split-Path $src -Leaf
                $destFile = Join-Path $destRoot $fileName
                $srcHash = (Get-FileHash -LiteralPath $src -Algorithm MD5).Hash
                if (Test-Path $destFile) {{
                    $dstHash = (Get-FileHash -LiteralPath $destFile -Algorithm MD5).Hash
                    if ($srcHash -ne $dstHash) {{
                        "  VERIFY FAIL: $fileName (hash mismatch)" | Out-File -Append '{prg_esc}'
                        $verifyFailed++
                    }} else {{
                        if ($verbose) {{ "  VERIFY OK: $fileName" | Out-File -Append '{prg_esc}' }}
                    }}
                }} else {{
                    "  VERIFY FAIL: $fileName (missing on USB)" | Out-File -Append '{prg_esc}'
                    $verifyFailed++
                }}
            }} elseif (Test-Path $src -PathType Container) {{
                $srcNorm = $src.TrimEnd('\', '/')
                $files = Get-ChildItem -LiteralPath $srcNorm -Recurse -File
                foreach ($f in $files) {{
                    $relPath = $f.FullName.Substring($srcNorm.Length + 1)
                    $destFile = Join-Path $destRoot $relPath
                    if (Test-Path $destFile) {{
                        $srcHash = (Get-FileHash -LiteralPath $f.FullName -Algorithm MD5).Hash
                        $dstHash = (Get-FileHash -LiteralPath $destFile -Algorithm MD5).Hash
                        if ($srcHash -ne $dstHash) {{
                            "  VERIFY FAIL: $relPath (hash mismatch)" | Out-File -Append '{prg_esc}'
                            $verifyFailed++
                        }} else {{
                            if ($verbose) {{ "  VERIFY OK: $relPath" | Out-File -Append '{prg_esc}' }}
                        }}
                    }} else {{
                        "  VERIFY FAIL: $relPath (missing on USB)" | Out-File -Append '{prg_esc}'
                        $verifyFailed++
                    }}
                }}
            }}
        }}
        if ($verifyFailed -eq 0) {{
            "Verification passed: all $totalCopied files match" | Out-File -Append '{prg_esc}'
        }} else {{
            "Verification FAILED: $verifyFailed file(s) differ" | Out-File -Append '{prg_esc}'
        }}
    }}

    if ($totalFailed -gt 0 -or $verifyFailed -gt 0) {{
        "OK:${{totalCopied}}:WARN:${{totalFailed}} copy errors, ${{verifyFailed}} verify errors" | Out-File -Append '{prg_esc}'
    }} else {{
        "OK:$totalCopied" | Out-File -Append '{prg_esc}'
    }}
}} catch {{
    "ERROR: $_" | Out-File -Append '{prg_esc}'
}}
"""
    with open(script_file, "w") as f:
        f.write(ps_script)

    try:
        cfg.log("  Requesting Administrator access...")
        proc = subprocess.Popen([
            "powershell.exe", "-NoProfile", "-Command",
            f"Start-Process -FilePath 'powershell.exe' "
            f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','{script_file}' "
            f"-Verb RunAs -Wait"
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        _poll_progress(proc, progress_file, cfg.log)

    except Exception as exc:
        cfg.log(f"[ERROR] {exc}")
    finally:
        for f in (script_file, progress_file):
            try:
                os.unlink(f)
            except OSError:
                pass


def _poll_progress(proc: subprocess.Popen[bytes], progress_file: str,
                   log: Callable[..., None]) -> None:
    """Poll a progress file while a subprocess runs, logging new lines.

    Reads the final status line after the process exits and logs the result.
    """
    import time
    lines_read = 0
    while proc.poll() is None:
        time.sleep(0.3)
        if os.path.exists(progress_file):
            try:
                with open(progress_file, "r", encoding="utf-8", errors="replace") as pf:
                    all_lines = pf.readlines()
                if len(all_lines) > lines_read:
                    for line in all_lines[lines_read:]:
                        stripped = line.rstrip()
                        if stripped:
                            log(f"  {stripped}")
                    lines_read = len(all_lines)
            except OSError:
                pass

    # Read remaining lines after process exits
    time.sleep(0.5)
    final_msg = ""
    if os.path.exists(progress_file):
        try:
            with open(progress_file, "r", encoding="utf-8", errors="replace") as pf:
                all_lines = pf.readlines()
            if len(all_lines) > lines_read:
                for line in all_lines[lines_read:]:
                    stripped = line.rstrip()
                    if stripped:
                        log(f"  {stripped}")
            final_msg = all_lines[-1].strip() if all_lines else ""
        except OSError:
            pass

    if final_msg.startswith("OK:"):
        count = final_msg.split(":")[1]
        log(f"[OK] Wrote {count} files to USB drive. "
            f"You can safely remove it.")
    elif final_msg.startswith("ERROR:"):
        log(f"[ERROR] {final_msg}")
    elif final_msg:
        log(f"  {final_msg}")
    else:
        log("[ERROR] Write subprocess produced no output. UAC may have been denied.")


def _write_usb_linux(
    cfg: Config, image_path: str, img_size: int, img_resolved: str,
    target: dict[str, str],
) -> None:
    """Write image to USB on Linux using dd."""
    target_path = target["path"]

    # Unmount any mounted partitions
    cfg.log(f"Unmounting {target_path} partitions...")
    _run(cfg, ["umount", f"{target_path}*"], check=False)
    for i in range(1, 10):
        _run(cfg, ["umount", f"{target_path}{i}"], check=False)
        _run(cfg, ["umount", f"{target_path}p{i}"], check=False)

    cfg.log(f"Writing {image_path} ({img_size // (1024*1024)}MB) to {target_path}...")
    dd_cmd = ["dd", f"if={img_resolved}", f"of={target_path}", "bs=4M", "conv=fsync"]
    if cfg.verbose:
        dd_cmd.insert(-1, "status=progress")
    r = _run(cfg, dd_cmd, check=False, verbose=True)

    if r.returncode != 0:
        cfg.log("  Retrying as root...")
        r = _run(cfg, [
            "dd", f"if={img_resolved}", f"of={target_path}",
            "bs=4M", "conv=fsync",
        ], check=False, verbose=True, as_root=True)

    if r.returncode != 0:
        cfg.log(f"[ERROR] dd failed: {r.stderr.strip()}")
        cfg.log("  You may need to run with sudo.")
        return

    _run(cfg, ["sync"], check=False)
    cfg.log(f"[OK] Wrote {img_size // (1024*1024)}MB to {target_path}. "
        f"You can safely remove the USB drive.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _write_usb_from_dir(cfg: Config, files: dict[str, str],
                        target: str) -> None:
    """Build and write to a USB device from collected files."""
    drive = _resolve_usb_target(cfg, target)
    if drive is None:
        return

    if not _usb_safety_checks(cfg, drive):
        return

    if not cfg.force:
        if not _cli_confirm_write(drive):
            cfg.log("Aborted.")
            return

    device = drive["path"]
    _unmount_device(cfg, device)

    if cfg.gpt:
        # GPT direct to device
        cfg.log(f"Writing GPT layout to {device}...")
        _write_gpt_to_device(cfg, files, device)
    elif _is_windows():
        _write_usb_windows(cfg, "", [], drive)
    else:
        # Build temp FAT32 image, then dd
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            cfg.log("Building FAT32 image...")
            build_img(cfg, files, tmp_path)
            img_size = os.path.getsize(tmp_path)
            img_resolved = _resolve(tmp_path)
            _write_usb_linux(cfg, tmp_path, img_size, img_resolved, drive)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _write_usb_from_image(cfg: Config, image_path: str,
                          target: str) -> None:
    """Write an existing image file to a USB device via dd."""
    if not os.path.isfile(image_path):
        cfg.log(f"Error: {image_path} not found")
        return

    drive = _resolve_usb_target(cfg, target)
    if drive is None:
        return

    if not _usb_safety_checks(cfg, drive):
        return

    if not cfg.force:
        if not _cli_confirm_write(drive):
            cfg.log("Aborted.")
            return

    _unmount_device(cfg, drive["path"])
    img_resolved = _resolve(image_path)
    img_size = os.path.getsize(image_path)
    _write_usb_linux(cfg, image_path, img_size, img_resolved, drive)


def _write_gpt_to_device(cfg: Config, files: dict[str, str],
                          device: str) -> None:
    """Write GPT layout directly to a USB device (no intermediate image).

    Requires root for sgdisk on a device and mount operations.
    """
    _check_root(cfg, "GPT USB write")
    ensure_tools(cfg, "gpt")

    content_mb = _calculate_content_size(files)
    esp_mb = max(int(content_mb * 1.3 + 10), 64)
    esp_label = cfg.esp_label[:11]

    # Collect data files upfront if needed
    data_files: dict[str, str] = {}
    data_label = cfg.data_label[:11]
    if cfg.data_dir:
        data_files = collect_files(cfg, cfg.data_dir, [])

    with tempfile.TemporaryDirectory() as staging:
        _stage_files(files, Path(staging))
        stg_resolved = _resolve(staging)

        # Partition the device directly
        cfg.log(f"  Creating GPT partition table on {device}...")
        _run(cfg, ["sgdisk", "-Z", device], verbose=cfg.verbose, as_root=True)
        _run(cfg, ["sgdisk", "-o", device], verbose=cfg.verbose, as_root=True)
        _run(cfg, ["sgdisk",
                   "-n", f"1:2048:+{esp_mb}M", "-t", "1:EF00",
                   "-c", f"1:{esp_label}",
                   device], verbose=True, as_root=True)

        if cfg.data_dir:
            _run(cfg, ["sgdisk",
                       "-n", "2:0:0", "-t", "2:0700",
                       "-c", f"2:{data_label}",
                       device], verbose=True, as_root=True)

        # Re-read partition table
        _run(cfg, ["partprobe", device], check=False, as_root=True)
        import time
        time.sleep(1)

        # Determine partition device naming: sdX1, sdXp1, or diskNs1 (macOS)
        if _is_macos():
            part_fmt = f"{device}s{{}}"
        elif Path(f"{device}1").exists() or Path(f"{device}1").is_block_device():
            part_fmt = f"{device}{{}}"
        else:
            part_fmt = f"{device}p{{}}"

        esp_part = part_fmt.format(1)
        cfg.log(f"  Formatting ESP ({esp_label}) on {esp_part}...")
        _format_partition(cfg, esp_part, cfg.fs_type, esp_label)
        cfg.log(f"  Copying {len(files)} files to ESP...")
        _populate_partition(cfg, stg_resolved, esp_part)

        if cfg.data_dir and data_files:
            data_part = part_fmt.format(2)
            data_staging = Path(staging) / "data"
            data_staging.mkdir()
            _stage_files(data_files, data_staging)
            data_stg = _resolve(str(data_staging))

            cfg.log(f"  Formatting data ({data_label}) on {data_part}...")
            _format_partition(cfg, data_part, cfg.fs_type, data_label)
            cfg.log(f"  Copying {len(data_files)} files to data...")
            _populate_partition(cfg, data_stg, data_part)

    _run(cfg, ["sync"], check=False)
    cfg.log(f"  [OK] USB drive {device} ready. You can safely remove it.")


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
            from mkimage_gui_dpg import gui_main
        except ImportError:
            from mkimage_gui import gui_main
        gui_main()
        return

    # Resolve --source / --target from new or old-style arguments
    source = args.source or args.source_dir
    target = args.target or args.output

    # Backward compat: --write-usb IMAGE → --source IMAGE --target usb
    if args.write_usb:
        source = source or args.write_usb
        target = target or "usb"

    cfg = Config(
        verbose=args.verbose,
        verify=args.verify,
        label=args.label,
        extra_mb=args.extra_mb,
        gpt=args.gpt or bool(args.data_dir),
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
        gpt_missing = check_tools_gpt()
        env = "WSL" if _is_windows() else "native"
        print(f"Environment: {env}")
        print(f"FAT32 (.img): {'OK' if not img_missing else 'MISSING: ' + ', '.join(img_missing)}")
        print(f"ISO   (.iso): {'OK' if not iso_missing else 'MISSING: ' + ', '.join(iso_missing)}")
        print(f"GPT   (.img): {'OK' if not gpt_missing else 'MISSING: ' + ', '.join(gpt_missing)}")
        all_missing = sorted(set(img_missing + iso_missing + gpt_missing))
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
            elif target_type == "img":
                print("Building FAT32 image...")
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


if __name__ == "__main__":
    main()
