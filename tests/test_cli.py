"""End-to-end CLI tests — runs mkimage.py as a subprocess."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from conftest import run_mkimage


class TestCliCheck:
    def test_check_exit_0(self) -> None:
        r = run_mkimage("--check")
        assert r.returncode == 0
        assert "FAT32" in r.stdout
        assert "ISO" in r.stdout

    def test_check_shows_environment(self) -> None:
        r = run_mkimage("--check")
        assert "Environment:" in r.stdout


class TestCliErrors:
    def test_missing_source(self) -> None:
        r = run_mkimage("--target", "out.img")
        assert r.returncode != 0

    def test_missing_target(self, sample_dir: Path) -> None:
        r = run_mkimage("--source", str(sample_dir))
        assert r.returncode != 0

    def test_invalid_extension(self, sample_dir: Path, tmp_path: Path) -> None:
        out = str(tmp_path / "out.txt")
        r = run_mkimage("--source", str(sample_dir), "--target", out)
        assert r.returncode != 0
        assert "img" in r.stderr.lower() or "iso" in r.stderr.lower()

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        out = str(tmp_path / "out.img")
        r = run_mkimage("--source", "/nonexistent/dir", "--target", out)
        assert r.returncode == 1
        assert "not a directory" in r.stderr.lower() or "error" in r.stderr.lower()


class TestCliBuild:
    def test_build_img(self, sample_dir: Path, tmp_path: Path) -> None:
        out = str(tmp_path / "out.img")
        r = run_mkimage("--source", str(sample_dir), "--target", out)
        assert r.returncode == 0
        assert os.path.exists(out)
        assert os.path.getsize(out) > 0
        assert "Done" in r.stdout

    def test_build_iso(self, sample_dir: Path, tmp_path: Path) -> None:
        out = str(tmp_path / "out.iso")
        r = run_mkimage("--source", str(sample_dir), "--target", out)
        assert r.returncode == 0
        assert os.path.exists(out)
        assert "Done" in r.stdout

    def test_include_flag(self, sample_dir: Path, tmp_path: Path) -> None:
        extra = tmp_path / "extra.txt"
        extra.write_text("extra content\n")
        out = str(tmp_path / "out.img")
        r = run_mkimage("--source", str(sample_dir), "--include", str(extra),
                        "--target", out)
        assert r.returncode == 0
        assert "extra.txt" in r.stdout

    def test_label_flag(self, sample_dir: Path, tmp_path: Path) -> None:
        out = str(tmp_path / "out.img")
        r = run_mkimage("--source", str(sample_dir), "--target", out,
                        "--label", "TESTLBL",
                        "--partition", "fat32::TESTLBL")
        assert r.returncode == 0
        # MBR image: use --list-image to verify label
        info = run_mkimage("--list-image", out)
        assert "TESTLBL" in info.stdout

    def test_verbose_flag(self, sample_dir: Path, tmp_path: Path) -> None:
        out = str(tmp_path / "out.img")
        r = run_mkimage("--source", str(sample_dir), "--target", out, "-v")
        assert r.returncode == 0
        # Pure Python path logs differently from dd/mcopy
        assert ("status=progress" in r.stdout or ">" in r.stdout
                or "Creating FAT32" in r.stdout or "Copying" in r.stdout)


class TestCliGptFlags:
    def test_check_shows_gpt(self) -> None:
        r = run_mkimage("--check")
        assert "GPT" in r.stdout

    def test_gpt_flag_recognized(self) -> None:
        r = run_mkimage("--help")
        assert "--gpt" in r.stdout

    def test_partition_flag_recognized(self) -> None:
        r = run_mkimage("--help")
        assert "--partition" in r.stdout


class TestCliSourceTarget:
    def test_source_target_img(self, sample_dir: Path, tmp_path: Path) -> None:
        out = str(tmp_path / "out.img")
        r = run_mkimage("--source", str(sample_dir), "--target", out)
        assert r.returncode == 0
        assert os.path.exists(out)
        assert "Done" in r.stdout

    def test_source_target_iso(self, sample_dir: Path, tmp_path: Path) -> None:
        out = str(tmp_path / "out.iso")
        r = run_mkimage("--source", str(sample_dir), "--target", out)
        assert r.returncode == 0
        assert os.path.exists(out)

    def test_missing_source_and_target(self) -> None:
        r = run_mkimage("--target", "/tmp/x.img")
        assert r.returncode != 0

    def test_image_to_file_rejects(self, sample_dir: Path, tmp_path: Path) -> None:
        """Cannot write an image source to a file target."""
        img = str(tmp_path / "src.img")
        Path(img).write_bytes(b"\x00" * 1024)
        r = run_mkimage("--source", img, "--target", str(tmp_path / "out.img"))
        assert r.returncode != 0
        assert "cannot" in r.stderr.lower() or "error" in r.stderr.lower()


class TestCliPhase3Flags:
    def test_list_drives(self) -> None:
        r = run_mkimage("--list-drives")
        assert r.returncode == 0

    def test_force_flag_recognized(self) -> None:
        r = run_mkimage("--help")
        assert "--force" in r.stdout

    def test_source_flag_recognized(self) -> None:
        r = run_mkimage("--help")
        assert "--source" in r.stdout
        assert "--target" in r.stdout

    def test_clone_examples_in_help(self) -> None:
        r = run_mkimage("--help")
        assert "Clone" in r.stdout or "clone" in r.stdout.lower()
