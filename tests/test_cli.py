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
        r = run_mkimage("-o", "out.img")
        assert r.returncode != 0

    def test_missing_output(self, sample_dir: Path) -> None:
        r = run_mkimage(str(sample_dir))
        assert r.returncode != 0

    def test_invalid_extension(self, sample_dir: Path, tmp_path: Path) -> None:
        out = str(tmp_path / "out.txt")
        r = run_mkimage(str(sample_dir), "-o", out)
        assert r.returncode != 0
        assert "img" in r.stderr.lower() or "iso" in r.stderr.lower()

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        out = str(tmp_path / "out.img")
        r = run_mkimage("/nonexistent/dir", "-o", out)
        assert r.returncode == 1
        assert "not a directory" in r.stderr.lower() or "error" in r.stderr.lower()


class TestCliBuild:
    def test_build_img(self, sample_dir: Path, tmp_path: Path) -> None:
        out = str(tmp_path / "out.img")
        r = run_mkimage(str(sample_dir), "-o", out)
        assert r.returncode == 0
        assert os.path.exists(out)
        assert os.path.getsize(out) > 0
        assert "Done" in r.stdout

    def test_build_iso(self, sample_dir: Path, tmp_path: Path) -> None:
        out = str(tmp_path / "out.iso")
        r = run_mkimage(str(sample_dir), "-o", out)
        assert r.returncode == 0
        assert os.path.exists(out)
        assert "Done" in r.stdout

    def test_include_flag(self, sample_dir: Path, tmp_path: Path) -> None:
        extra = tmp_path / "extra.txt"
        extra.write_text("extra content\n")
        out = str(tmp_path / "out.img")
        r = run_mkimage(str(sample_dir), "--include", str(extra), "-o", out)
        assert r.returncode == 0
        # Verify extra file listed in output
        assert "extra.txt" in r.stdout

    def test_label_flag(self, sample_dir: Path, tmp_path: Path) -> None:
        out = str(tmp_path / "out.img")
        r = run_mkimage(str(sample_dir), "-o", out, "--label", "TESTLBL")
        assert r.returncode == 0
        # Verify via minfo
        info = subprocess.run(
            ["minfo", "-i", out, "::"],
            capture_output=True, text=True,
        )
        assert "TESTLBL" in info.stdout

    def test_verbose_flag(self, sample_dir: Path, tmp_path: Path) -> None:
        out = str(tmp_path / "out.img")
        r = run_mkimage(str(sample_dir), "-o", out, "-v")
        assert r.returncode == 0
        # Verbose should include per-file rsync output
        assert "status=progress" in r.stdout or "sending" in r.stdout or ">" in r.stdout
