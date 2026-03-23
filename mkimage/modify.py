"""Modify existing FAT32 images (add/remove files)."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from mkimage.platform import _resolve, _run, _which

if TYPE_CHECKING:
    from mkimage import Config


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
