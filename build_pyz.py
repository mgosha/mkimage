#!/usr/bin/env python3
"""Build mkimage.pyz — a single-file cross-platform distribution.

Bundles mkimage.py and mkimage_gui.py into a zipapp that runs on
Linux, macOS, and Windows with Python 3.7+.

Usage:
    python3 build_pyz.py

Output:
    mkimage.pyz   (~40KB, runs with: python3 mkimage.pyz <args>)

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

MODULES = [
    "mkimage.py",
    "mkimage_gui.py",
]


def build() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # Copy the Python modules
        for module in MODULES:
            src = os.path.join(SCRIPT_DIR, module)
            if not os.path.isfile(src):
                print(f"ERROR: {module} not found in {SCRIPT_DIR}")
                sys.exit(1)
            shutil.copy2(src, os.path.join(tmp, module))

        # Create __main__.py entry point
        main_py = os.path.join(tmp, "__main__.py")
        with open(main_py, "w") as f:
            f.write("from mkimage import main\nmain()\n")

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
