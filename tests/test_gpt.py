"""GPT image creation tests.

Structure tests (sgdisk on files) run without root.
Full integration tests (losetup + mount) require root.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mkimage import (
    Config, PartitionSpec, build_gpt_img, collect_files,
)


def _sgdisk_info(img: str) -> str:
    """Get partition table info via sgdisk -p."""
    r = subprocess.run(["sgdisk", "-p", img], capture_output=True, text=True)
    return r.stdout


# ---------------------------------------------------------------------------
# GPT structure tests -- sgdisk works on image files without root
# ---------------------------------------------------------------------------

class TestGptStructure:
    def test_single_esp_partition(self, tmp_path: Path) -> None:
        """Verify sgdisk creates a single ESP partition."""
        img = str(tmp_path / "test.img")
        subprocess.run(["dd", "if=/dev/zero", f"of={img}", "bs=1M",
                        "count=0", "seek=66"], check=True, capture_output=True)
        subprocess.run(["sgdisk", "-Z", img], check=True, capture_output=True)
        subprocess.run(["sgdisk", "-o", img], check=True, capture_output=True)
        subprocess.run(["sgdisk", "-n", "1:2048:+64M", "-t", "1:EF00",
                        "-c", "1:ESP", img], check=True, capture_output=True)

        info = _sgdisk_info(img)
        assert "EF00" in info
        assert "ESP" in info

    def test_dual_partition(self, tmp_path: Path) -> None:
        """Verify two-partition GPT layout (ESP + data)."""
        img = str(tmp_path / "dual.img")
        subprocess.run(["dd", "if=/dev/zero", f"of={img}", "bs=1M",
                        "count=0", "seek=130"], check=True, capture_output=True)
        subprocess.run(["sgdisk", "-Z", img], check=True, capture_output=True)
        subprocess.run(["sgdisk", "-o", img], check=True, capture_output=True)
        subprocess.run(["sgdisk", "-n", "1:2048:+64M", "-t", "1:EF00",
                        "-c", "1:ESP", img], check=True, capture_output=True)
        subprocess.run(["sgdisk", "-n", "2:0:0", "-t", "2:0700",
                        "-c", "2:DATA", img], check=True, capture_output=True)

        info = _sgdisk_info(img)
        assert "EF00" in info
        assert "0700" in info
        assert "ESP" in info
        assert "DATA" in info

    def test_sparse_image_is_small(self, tmp_path: Path) -> None:
        """Verify sparse dd creates a small file on disk."""
        img = str(tmp_path / "sparse.img")
        subprocess.run(["dd", "if=/dev/zero", f"of={img}", "bs=1M",
                        "count=0", "seek=256"], check=True, capture_output=True)
        # Apparent size is 256MB but actual disk usage should be tiny
        stat = os.stat(img)
        assert stat.st_size == 256 * 1024 * 1024  # apparent size
        assert stat.st_blocks * 512 < 1024 * 1024  # actual < 1MB


# ---------------------------------------------------------------------------
# Full GPT integration tests -- require root for losetup + mount
# ---------------------------------------------------------------------------

class TestBuildGptImg:
    @pytest.mark.needs_root
    def test_creates_gpt_image(self, sample_dir: Path, tmp_path: Path) -> None:
        cfg = Config(gpt=True, partitions=[PartitionSpec("esp", "", "ESP")])
        files = collect_files(cfg, str(sample_dir), [])
        out = str(tmp_path / "gpt.img")
        build_gpt_img(cfg, files, out)
        assert os.path.exists(out)
        info = _sgdisk_info(out)
        assert "EF00" in info
        assert "ESP" in info

    @pytest.mark.needs_root
    def test_minimum_esp_size(self, sample_dir: Path, tmp_path: Path) -> None:
        """ESP should be at least 64MB even for tiny content."""
        cfg = Config(gpt=True, partitions=[PartitionSpec("esp", "", "ESP")])
        files = collect_files(cfg, str(sample_dir), [])
        out = str(tmp_path / "gpt.img")
        build_gpt_img(cfg, files, out)
        info = _sgdisk_info(out)
        assert "64.0 MiB" in info

    @pytest.mark.needs_root
    def test_custom_esp_label(self, sample_dir: Path, tmp_path: Path) -> None:
        cfg = Config(gpt=True, partitions=[PartitionSpec("esp", "", "MYESP")])
        files = collect_files(cfg, str(sample_dir), [])
        out = str(tmp_path / "gpt.img")
        build_gpt_img(cfg, files, out)
        info = _sgdisk_info(out)
        assert "MYESP" in info

    @pytest.mark.needs_root
    def test_esp_contains_files(self, sample_dir: Path, tmp_path: Path) -> None:
        """Mount the ESP and verify files are present."""
        cfg = Config(gpt=True, partitions=[PartitionSpec("esp", "", "ESP")])
        files = collect_files(cfg, str(sample_dir), [])
        out = str(tmp_path / "gpt.img")
        build_gpt_img(cfg, files, out)

        r = subprocess.run(
            ["losetup", "--find", "--show", "--partscan", out],
            capture_output=True, text=True, check=True,
        )
        loop = r.stdout.strip()
        try:
            mnt = tmp_path / "mnt"
            mnt.mkdir()
            subprocess.run(["mount", f"{loop}p1", str(mnt)], check=True)
            try:
                assert (mnt / "startup.nsh").exists()
                assert (mnt / "EFI" / "BOOT" / "BOOTX64.EFI").exists()
                assert (mnt / "tools" / "readme.txt").exists()
                assert (mnt / "startup.nsh").read_text() == "echo Hello\n"
            finally:
                subprocess.run(["umount", str(mnt)], check=False)
        finally:
            subprocess.run(["losetup", "-d", loop], check=False)

    @pytest.mark.needs_root
    def test_verbose_logging(self, sample_dir: Path, tmp_path: Path) -> None:
        messages: list[str] = []
        cfg = Config(
            verbose=True, gpt=True, log=lambda msg: messages.append(msg),
            partitions=[PartitionSpec("esp", "", "ESP")],
        )
        files = collect_files(cfg, str(sample_dir), [])
        out = str(tmp_path / "gpt.img")
        build_gpt_img(cfg, files, out)
        assert any("GPT image" in m for m in messages)
        assert any("[OK]" in m for m in messages)


class TestBuildGptDataImg:
    @pytest.mark.needs_root
    def test_dual_partition(self, sample_dir: Path, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "config.txt").write_text("key=value\n")
        (data_dir / "README.md").write_text("Data partition\n")

        cfg = Config(gpt=True, partitions=[
            PartitionSpec("esp", "", "ESP"),
            PartitionSpec("fat32", "", "DATA", str(data_dir)),
        ])
        esp_files = collect_files(cfg, str(sample_dir), [])
        out = str(tmp_path / "dual.img")
        build_gpt_img(cfg, esp_files, out)

        info = _sgdisk_info(out)
        assert "EF00" in info
        assert "0700" in info

    @pytest.mark.needs_root
    def test_data_partition_contains_files(self, sample_dir: Path,
                                            tmp_path: Path) -> None:
        """Mount both partitions and verify files."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "config.txt").write_text("key=value\n")

        cfg = Config(gpt=True, partitions=[
            PartitionSpec("esp", "", "ESP"),
            PartitionSpec("fat32", "", "DATA", str(data_dir)),
        ])
        esp_files = collect_files(cfg, str(sample_dir), [])
        out = str(tmp_path / "dual.img")
        build_gpt_img(cfg, esp_files, out)

        r = subprocess.run(
            ["losetup", "--find", "--show", "--partscan", out],
            capture_output=True, text=True, check=True,
        )
        loop = r.stdout.strip()
        try:
            mnt = tmp_path / "mnt"
            mnt.mkdir()

            # Check ESP
            subprocess.run(["mount", f"{loop}p1", str(mnt)], check=True)
            try:
                assert (mnt / "startup.nsh").exists()
                assert (mnt / "EFI" / "BOOT" / "BOOTX64.EFI").exists()
            finally:
                subprocess.run(["umount", str(mnt)], check=False)

            # Check data partition
            subprocess.run(["mount", f"{loop}p2", str(mnt)], check=True)
            try:
                assert (mnt / "config.txt").exists()
                assert (mnt / "config.txt").read_text() == "key=value\n"
            finally:
                subprocess.run(["umount", str(mnt)], check=False)
        finally:
            subprocess.run(["losetup", "-d", loop], check=False)

    @pytest.mark.needs_root
    def test_custom_labels(self, sample_dir: Path, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "x.txt").write_text("x\n")

        cfg = Config(gpt=True, partitions=[
            PartitionSpec("esp", "", "BOOT"),
            PartitionSpec("fat32", "", "FILES", str(data_dir)),
        ])
        esp_files = collect_files(cfg, str(sample_dir), [])
        out = str(tmp_path / "labels.img")
        build_gpt_img(cfg, esp_files, out)

        info = _sgdisk_info(out)
        assert "BOOT" in info
        assert "FILES" in info

    @pytest.mark.needs_root
    def test_fixed_data_size(self, sample_dir: Path, tmp_path: Path) -> None:
        """Verify fixed data partition size."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "x.txt").write_text("x\n")

        cfg = Config(gpt=True, partitions=[
            PartitionSpec("esp", "", "ESP"),
            PartitionSpec("fat32", "128M", "DATA", str(data_dir)),
        ])
        esp_files = collect_files(cfg, str(sample_dir), [])
        out = str(tmp_path / "fixed.img")
        build_gpt_img(cfg, esp_files, out)

        info = _sgdisk_info(out)
        # Total should be ESP (64MB) + data (128MB) + 2MB overhead = 194MB
        assert "0700" in info
        # Image should be ~194MB
        size_mb = os.path.getsize(out) // (1024 * 1024)
        assert size_mb >= 190
