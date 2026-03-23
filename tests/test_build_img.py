"""Integration tests for FAT32 image creation.

Most tests verify images using mtools (mdir, mcopy, minfo) — no root needed.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mkimage import Config, PartitionSpec, build_img, build_iso, collect_files


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
    cfg = Config(partitions=[PartitionSpec()])
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
        cfg = Config(label="MYTOOLS",
                     partitions=[PartitionSpec("fat32", "", "MYTOOLS")])
        files = collect_files(cfg, str(sample_dir), [])
        out = tmp_path / "labeled.img"
        build_img(cfg, files, str(out))
        info = _minfo(str(out))
        assert "MYTOOLS" in info

    def test_auto_size_scales(self, tmp_path: Path) -> None:
        """Larger input produces larger image when extra is above minimum."""
        src = tmp_path / "src"
        src.mkdir()
        # Create a 5MB file
        (src / "big.bin").write_bytes(b"\x00" * (5 * 1024 * 1024))

        cfg = Config(partitions=[PartitionSpec("fat32", "+50M")])
        files = collect_files(cfg, str(src), [])
        out = tmp_path / "big.img"
        build_img(cfg, files, str(out))
        # Should be at least 5MB content + 50MB extra = 55MB
        assert out.stat().st_size >= 55 * 1024 * 1024

    def test_verbose_logging(self, sample_dir: Path, tmp_path: Path) -> None:
        messages: list[str] = []
        cfg = Config(verbose=True, log=lambda msg: messages.append(msg),
                     partitions=[PartitionSpec()])
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


class TestBuildImgRewrite:
    """Test overwriting an existing output file with a different format.

    Simulates real-world scenarios where a file or device previously held
    ISO 9660, a different-sized FAT32 image, or garbage data, and needs
    to be cleanly rewritten.
    """

    def test_iso_then_img(self, sample_dir: Path, tmp_path: Path) -> None:
        """Write ISO, then overwrite same path with FAT32 image."""
        out = str(tmp_path / "rewrite.img")
        cfg = Config(label="ISOFIRST")
        files = collect_files(cfg, str(sample_dir), [])

        # Write ISO first
        build_iso(cfg, files, out)
        ft = _file_type(out)
        assert "ISO 9660" in ft

        # Overwrite with FAT32
        cfg2 = Config(label="IMGAFTER", partitions=[PartitionSpec()])
        build_img(cfg2, files, out)
        ft2 = _file_type(out)
        assert "FAT" in ft2
        # Verify content is correct — no ISO remnants
        listing = _mdir(out)
        assert "startup" in listing.lower() or "STARTUP" in listing
        content = _mcopy_extract(out, "startup.nsh")
        assert b"echo Hello" in content

    def test_img_then_iso(self, sample_dir: Path, tmp_path: Path) -> None:
        """Write FAT32 image, then overwrite same path with ISO."""
        out = str(tmp_path / "rewrite.iso")
        cfg = Config(label="IMGFIRST", partitions=[PartitionSpec()])
        files = collect_files(cfg, str(sample_dir), [])

        # Write FAT32 first
        build_img(cfg, files, out)
        ft = _file_type(out)
        assert "FAT" in ft

        # Overwrite with ISO
        cfg2 = Config(label="ISOAFTER")
        build_iso(cfg2, files, out)
        ft2 = _file_type(out)
        assert "ISO 9660" in ft2

    def test_small_img_then_large_img(self, sample_dir: Path, tmp_path: Path) -> None:
        """Write small image, then overwrite with larger image."""
        out = str(tmp_path / "grow.img")
        cfg = Config(label="SMALL",
                     partitions=[PartitionSpec("fat32", "+8M", "SMALL")])
        files = collect_files(cfg, str(sample_dir), [])
        build_img(cfg, files, out)
        small_size = os.path.getsize(out)

        # Write larger image over it
        cfg2 = Config(label="LARGE",
                      partitions=[PartitionSpec("fat32", "+80M", "LARGE")])
        build_img(cfg2, files, out)
        large_size = os.path.getsize(out)
        assert large_size > small_size
        # Verify content
        listing = _mdir(out)
        assert "startup" in listing.lower() or "STARTUP" in listing
        info = _minfo(out)
        assert "LARGE" in info

    def test_large_img_then_small_img(self, sample_dir: Path, tmp_path: Path) -> None:
        """Write large image, then overwrite with smaller image.

        Ensures the output file is truncated -- no leftover data from the
        larger image bleeds through.
        """
        out = str(tmp_path / "shrink.img")
        cfg = Config(label="BIG",
                     partitions=[PartitionSpec("fat32", "+80M", "BIG")])
        files = collect_files(cfg, str(sample_dir), [])
        build_img(cfg, files, out)
        big_size = os.path.getsize(out)

        # Write smaller image over it
        cfg2 = Config(label="SMALL",
                      partitions=[PartitionSpec("fat32", "+8M", "SMALL")])
        build_img(cfg2, files, out)
        small_size = os.path.getsize(out)
        assert small_size <= big_size
        # Verify content, not garbage from old image
        ft = _file_type(out)
        assert "FAT" in ft
        listing = _mdir(out)
        assert "startup" in listing.lower() or "STARTUP" in listing

    def test_garbage_then_img(self, sample_dir: Path) -> None:
        """Write random garbage, then create a valid FAT32 image over it."""
        import tempfile as _tf
        with _tf.TemporaryDirectory() as out_dir:
            out = str(Path(out_dir) / "garbage.img")
            # Write 50MB of random data
            subprocess.run(
                ["dd", "if=/dev/urandom", f"of={out}", "bs=1M", "count=50"],
                check=True, capture_output=True,
            )
            assert os.path.getsize(out) == 50 * 1024 * 1024

            cfg = Config(label="CLEANED", partitions=[PartitionSpec()])
            files = collect_files(cfg, str(sample_dir), [])
            build_img(cfg, files, out)
            ft = _file_type(out)
            assert "FAT" in ft
            content = _mcopy_extract(out, "startup.nsh")
            assert b"echo Hello" in content

    def test_different_label_overwrites(self, sample_dir: Path, tmp_path: Path) -> None:
        """Verify old label doesn't persist when rewriting."""
        out = str(tmp_path / "label.img")
        cfg = Config(label="OLDLABEL",
                     partitions=[PartitionSpec("fat32", "", "OLDLABEL")])
        files = collect_files(cfg, str(sample_dir), [])
        build_img(cfg, files, out)
        info = _minfo(out)
        assert "OLDLABEL" in info

        cfg2 = Config(label="NEWLABEL",
                      partitions=[PartitionSpec("fat32", "", "NEWLABEL")])
        build_img(cfg2, files, out)
        info2 = _minfo(out)
        assert "NEWLABEL" in info2
        assert "OLDLABEL" not in info2
