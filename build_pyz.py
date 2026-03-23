#!/usr/bin/env python3
"""Build mkimage.pyz — a single-file cross-platform distribution.

Bundles the mkimage package into a zipapp that runs on
Linux, macOS, and Windows with Python 3.7+.

Usage:
    python3 build_pyz.py

Output:
    mkimage.pyz   (runs with: python3 mkimage.pyz <args>)

On Windows, .pyz files are associated with Python by default, so
double-clicking launches the GUI. On Linux/macOS, run directly:
    python3 mkimage.pyz build/binaries/ -o output.img
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import zipapp

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(SCRIPT_DIR, "mkimage.pyz")
PACKAGE_DIR = os.path.join(SCRIPT_DIR, "mkimage")


def build() -> None:
    if not os.path.isdir(PACKAGE_DIR):
        print(f"ERROR: mkimage/ package not found in {SCRIPT_DIR}")
        sys.exit(1)

    with tempfile.TemporaryDirectory() as tmp:
        # Copy the mkimage package
        shutil.copytree(
            PACKAGE_DIR,
            os.path.join(tmp, "mkimage"),
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

        # Create __main__.py entry point at top level
        main_py = os.path.join(tmp, "__main__.py")
        with open(main_py, "w") as f:
            f.write("from mkimage.cli import main\nmain()\n")

        # Build the zipapp
        zipapp.create_archive(
            tmp,
            target=OUTPUT,
            interpreter="/usr/bin/env python3",
            compressed=True,
        )

    size = os.path.getsize(OUTPUT)
    print(f"Built {OUTPUT} ({size // 1024}KB)")
    print(f"  Run: python3 {os.path.basename(OUTPUT)} --help")


if __name__ == "__main__":
    build()
