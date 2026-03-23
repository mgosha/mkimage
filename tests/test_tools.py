"""Tests for tool detection and ensure_tools."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from mkimage import (
    Config, check_tools_gpt, check_tools_img, check_tools_iso, ensure_tools,
)


class TestCheckTools:
    def test_img_tools_present(self) -> None:
        """On this host, all FAT32 tools should be available."""
        assert check_tools_img() == []

    def test_iso_tools_present(self) -> None:
        """On this host, xorriso or genisoimage should be available."""
        assert check_tools_iso() == []

    def test_img_mock_missing(self) -> None:
        """When _which returns False for mkfs.vfat, it appears in missing list."""
        original_which = __import__("mkimage")._which

        def fake_which(tool: str) -> bool:
            if tool == "mkfs.vfat":
                return False
            return original_which(tool)

        with patch("mkimage._which", side_effect=fake_which):
            missing = check_tools_img()
        assert "mkfs.vfat" in missing

    def test_iso_mock_both_missing(self) -> None:
        """When both xorriso and genisoimage are missing, returns ['xorriso']."""
        def fake_which(tool: str) -> bool:
            if tool in ("xorriso", "genisoimage"):
                return False
            return True

        with patch("mkimage._which", side_effect=fake_which):
            missing = check_tools_iso()
        assert missing == ["xorriso"]


class TestEnsureTools:
    def test_no_error_when_present(self) -> None:
        cfg = Config()
        ensure_tools(cfg, "img")  # should not raise
        ensure_tools(cfg, "iso")  # should not raise

    def test_raises_when_missing(self) -> None:
        """When tools are missing and install fails, RuntimeError is raised."""
        def fake_which(tool: str) -> bool:
            if tool in ("dd", "mkfs.vfat", "rsync"):
                return False
            return True

        cfg = Config()
        with patch("mkimage._which", side_effect=fake_which), \
             patch("mkimage._install_packages", return_value=False):
            with pytest.raises(RuntimeError, match="install"):
                ensure_tools(cfg, "img")


class TestCheckToolsGpt:
    def test_gpt_tools_present(self) -> None:
        """On this host, sgdisk and losetup should be available."""
        assert check_tools_gpt() == []

    def test_gpt_mock_missing_sgdisk(self) -> None:
        original_which = __import__("mkimage")._which

        def fake_which(tool: str) -> bool:
            if tool == "sgdisk":
                return False
            return original_which(tool)

        with patch("mkimage._which", side_effect=fake_which):
            missing = check_tools_gpt()
        assert "sgdisk" in missing
