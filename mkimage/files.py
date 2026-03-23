"""File collection, staging, and size calculation."""
from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mkimage import Config


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


def _interpret_size(spec: str, content_mb: int, is_esp: bool = False) -> int:
    """Interpret partition size spec. Returns MB.

    "" = auto (content * 1.3 + 10, min 64 for ESP, min 40 for others)
    "+32M" = content + 32MB extra (min 40)
    "64M" = fixed 64MB
    "4G" = fixed 4096MB
    "0" = rest of disk (return 0, caller handles)
    """
    min_size = 64 if is_esp else 40
    if not spec:
        return max(int(content_mb * 1.3 + 10), min_size)
    if spec.startswith("+"):
        extra = _parse_size(spec[1:])
        return max(min_size, content_mb + extra)
    if spec == "0":
        return 0
    return _parse_size(spec)
