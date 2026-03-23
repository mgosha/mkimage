"""Integration tests for ISO image creation."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mkimage import Config, build_iso, collect_files


def _file_type(path: str) -> str:
    r = subprocess.run(["file", path], capture_output=True, text=True)
    return r.stdout


def _isoinfo_listing(iso: str) -> str:
    r = subprocess.run(
        ["isoinfo", "-l", "-i", iso],
        capture_output=True, text=True,
    )
    return r.stdout


def _isoinfo_volume(iso: str) -> str:
    r = subprocess.run(
        ["isoinfo", "-d", "-i", iso],
        capture_output=True, text=True,
    )
    return r.stdout


def _isoinfo_extract(iso: str, path: str) -> bytes:
    """Extract a file from an ISO image."""
    r = subprocess.run(
        ["isoinfo", "-i", iso, "-x", path],
        capture_output=True,
    )
    return r.stdout


@pytest.fixture
def built_iso(sample_dir: Path, tmp_path: Path) -> Path:
    cfg = Config()
    files = collect_files(cfg, str(sample_dir), [])
    out = tmp_path / "test.iso"
    build_iso(cfg, files, str(out))
    return out


class TestBuildIso:
    def test_creates_file(self, built_iso: Path) -> None:
        assert built_iso.exists()
        assert built_iso.stat().st_size > 0

    def test_iso_format(self, built_iso: Path) -> None:
        ft = _file_type(str(built_iso))
        assert "ISO 9660" in ft

    def test_contains_files(self, built_iso: Path) -> None:
        listing = _isoinfo_listing(str(built_iso))
        assert "STARTUP" in listing.upper()
        assert "README" in listing.upper()

    def test_nested_dirs(self, built_iso: Path) -> None:
        listing = _isoinfo_listing(str(built_iso))
        assert "BOOTX64" in listing.upper()

    def test_custom_label(self, sample_dir: Path, tmp_path: Path) -> None:
        cfg = Config(label="TESTLABEL")
        files = collect_files(cfg, str(sample_dir), [])
        out = tmp_path / "labeled.iso"
        build_iso(cfg, files, str(out))
        vol_info = _isoinfo_volume(str(out))
        assert "TESTLABEL" in vol_info

    def test_file_content(self, built_iso: Path) -> None:
        # ISO paths use uppercase and trailing versions like ";1"
        # Try the Joliet path first
        content = _isoinfo_extract(str(built_iso), "/startup.nsh;1")
        if not content:
            content = _isoinfo_extract(str(built_iso), "/STARTUP.NSH;1")
        assert b"echo Hello" in content


class TestBuildIsoUdfBridge:
    def test_udf_bridge_creates_file(self, sample_dir: Path, tmp_path: Path) -> None:
        cfg = Config(udf_bridge=True)
        files = collect_files(cfg, str(sample_dir), [])
        out = tmp_path / "bridge.iso"
        build_iso(cfg, files, str(out))
        assert out.exists()
        assert out.stat().st_size > 0

    def test_udf_bridge_format(self, sample_dir: Path, tmp_path: Path) -> None:
        cfg = Config(udf_bridge=True)
        files = collect_files(cfg, str(sample_dir), [])
        out = tmp_path / "bridge.iso"
        build_iso(cfg, files, str(out))
        ft = _file_type(str(out))
        assert "ISO 9660" in ft

    def test_udf_bridge_with_hybrid(self, sample_dir: Path, tmp_path: Path) -> None:
        cfg = Config(udf_bridge=True, iso_hybrid=True)
        files = collect_files(cfg, str(sample_dir), [])
        out = tmp_path / "hybrid_bridge.iso"
        build_iso(cfg, files, str(out))
        assert out.exists()
        ft = _file_type(str(out))
        assert "ISO 9660" in ft
