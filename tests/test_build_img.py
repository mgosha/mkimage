"""Integration tests for FAT32 image creation.

Most tests verify images using mtools (mdir, mcopy, minfo) — no root needed.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mkimage import Config, build_img, collect_files


def _file_type(path: str) -> str:
    """Get file type string from file(1)."""
    r = subprocess.run(["file", path], capture_output=True, text=True)
    return r.stdout


def _mdir(img: str, path: str = "::") -> str:
    """List directory contents in a FAT image using mdir."""
    r = subprocess.run(
        ["mdir", "-i", img, path],
        capture_output=True, text=True,
    )
    return r.stdout


def _mcopy_extract(img: str, img_path: str) -> bytes:
    """Extract a file from a FAT image to stdout using mcopy."""
    r = subprocess.run(
        ["mcopy", "-i", img, f"::{img_path}", "-"],
        capture_output=True,
    )
    return r.stdout


def _minfo(img: str) -> str:
    """Get filesystem info from a FAT image."""
    r = subprocess.run(
        ["minfo", "-i", img, "::"],
        capture_output=True, text=True,
    )
    return r.stdout


@pytest.fixture
def built_img(sample_dir: Path, tmp_path: Path) -> Path:
    """Build a FAT32 image from sample_dir and return its path."""
    cfg = Config()
    files = collect_files(cfg, str(sample_dir), [])
    out = tmp_path / "test.img"
    build_img(cfg, files, str(out))
    return out


class TestBuildImg:
    def test_creates_file(self, built_img: Path) -> None:
        assert built_img.exists()
        assert built_img.stat().st_size > 0

    def test_minimum_size(self, built_img: Path) -> None:
        # Minimum image size is 40MB
        assert built_img.stat().st_size >= 40 * 1024 * 1024

    def test_fat32_format(self, built_img: Path) -> None:
        ft = _file_type(str(built_img))
        assert "FAT" in ft or "filesystem" in ft.lower()

    def test_contains_files(self, built_img: Path) -> None:
        listing = _mdir(str(built_img))
        assert "startup" in listing.lower() or "STARTUP" in listing

    def test_nested_dirs(self, built_img: Path) -> None:
        listing = _mdir(str(built_img), "::/EFI/BOOT")
        assert "BOOTX64" in listing.upper()

    def test_file_content(self, built_img: Path) -> None:
        content = _mcopy_extract(str(built_img), "startup.nsh")
        assert b"echo Hello" in content

    def test_binary_content(self, built_img: Path) -> None:
        content = _mcopy_extract(str(built_img), "EFI/BOOT/BOOTX64.EFI")
        assert content == b"\x90" * 1024

    def test_custom_label(self, sample_dir: Path, tmp_path: Path) -> None:
        cfg = Config(label="MYTOOLS")
        files = collect_files(cfg, str(sample_dir), [])
        out = tmp_path / "labeled.img"
        build_img(cfg, files, str(out))
        info = _minfo(str(out))
        assert "MYTOOLS" in info

    def test_auto_size_scales(self, tmp_path: Path) -> None:
        """Larger input produces larger image when extra_mb pushes it above minimum."""
        src = tmp_path / "src"
        src.mkdir()
        # Create a 5MB file
        (src / "big.bin").write_bytes(b"\x00" * (5 * 1024 * 1024))

        cfg = Config(extra_mb=50)
        files = collect_files(cfg, str(src), [])
        out = tmp_path / "big.img"
        build_img(cfg, files, str(out))
        # Should be at least 5MB content + 50MB extra = 55MB
        assert out.stat().st_size >= 55 * 1024 * 1024

    def test_verbose_logging(self, sample_dir: Path, tmp_path: Path) -> None:
        messages: list[str] = []
        cfg = Config(verbose=True, log=lambda msg: messages.append(msg))
        files = collect_files(cfg, str(sample_dir), [])
        out = tmp_path / "verbose.img"
        build_img(cfg, files, str(out))
        # Verbose should produce more output
        assert len(messages) > 2

    @pytest.mark.needs_root
    def test_mount_path(self, built_img: Path, tmp_path: Path) -> None:
        """Mount the image and verify files via filesystem (requires root)."""
        mnt = tmp_path / "mnt"
        mnt.mkdir()
        try:
            subprocess.run(
                ["mount", "-o", "loop", str(built_img), str(mnt)],
                check=True,
            )
            assert (mnt / "startup.nsh").exists()
            assert (mnt / "EFI" / "BOOT" / "BOOTX64.EFI").exists()
            assert (mnt / "tools" / "readme.txt").exists()
            assert (mnt / "startup.nsh").read_text() == "echo Hello\n"
        finally:
            subprocess.run(["umount", str(mnt)], check=False)
