"""Pure unit tests — no root, no tools, no network."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mkimage import (
    Config, PartitionSpec, _calculate_content_size, _interpret_size,
    _parse_size, _resolve, _shell_quote, _stage_files, collect_files,
)


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

class TestConfig:
    def test_defaults(self) -> None:
        cfg = Config()
        assert cfg.verbose is False
        assert cfg.verify is False
        assert cfg.gpt is False
        assert cfg.label == "UEFITOOLS"
        assert cfg.force is False
        assert cfg.partitions == []

    def test_custom_values(self) -> None:
        parts = [PartitionSpec("esp", "", "ESP")]
        cfg = Config(verbose=True, label="MYTOOLS", gpt=True, partitions=parts)
        assert cfg.verbose is True
        assert cfg.label == "MYTOOLS"
        assert cfg.gpt is True
        assert len(cfg.partitions) == 1
        assert cfg.partitions[0].fs_type == "esp"

    def test_log_default_is_print(self) -> None:
        cfg = Config()
        assert cfg.log is print

    def test_log_captures(self) -> None:
        messages: list[str] = []
        cfg = Config(log=lambda msg: messages.append(msg))
        cfg.log("hello")
        cfg.log("world")
        assert messages == ["hello", "world"]


class TestPartitionSpec:
    def test_defaults(self) -> None:
        ps = PartitionSpec()
        assert ps.fs_type == "fat32"
        assert ps.size == ""
        assert ps.label == "UEFITOOLS"
        assert ps.source_dir == ""

    def test_esp(self) -> None:
        ps = PartitionSpec("esp", "64M", "ESP")
        assert ps.fs_type == "esp"
        assert ps.size == "64M"
        assert ps.label == "ESP"

    def test_with_source_dir(self) -> None:
        ps = PartitionSpec("fat32", "0", "DATA", "/tmp/data")
        assert ps.source_dir == "/tmp/data"


class TestInterpretSize:
    def test_auto_esp(self) -> None:
        result = _interpret_size("", 10, is_esp=True)
        assert result >= 64  # min for ESP

    def test_auto_regular(self) -> None:
        result = _interpret_size("", 10, is_esp=False)
        assert result >= 40  # min for non-ESP

    def test_extra(self) -> None:
        result = _interpret_size("+32M", 10)
        assert result == 42  # 10 + 32

    def test_extra_min(self) -> None:
        result = _interpret_size("+1M", 1)
        assert result >= 40  # min 40

    def test_fixed_mb(self) -> None:
        assert _interpret_size("64M", 10) == 64

    def test_fixed_gb(self) -> None:
        assert _interpret_size("4G", 10) == 4096

    def test_rest_of_disk(self) -> None:
        assert _interpret_size("0", 10) == 0


# ---------------------------------------------------------------------------
# _shell_quote
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("input_str,expected", [
    ("hello", "hello"),
    ("/usr/bin/tool", "/usr/bin/tool"),
    ("key=value", "key=value"),
    ("if=/dev/zero", "if=/dev/zero"),
    ("-flag", "-flag"),
    ("a.b_c", "a.b_c"),
    ("a b", "'a b'"),
    ("it's", "'it'\\''s'"),
    ("a;b", "'a;b'"),
    ("$HOME", "'$HOME'"),
    ("a`b", "'a`b'"),
    ("a&b", "'a&b'"),
    ("a|b", "'a|b'"),
    ("a(b)", "'a(b)'"),
], ids=[
    "simple", "path", "equals", "dev-path", "flag", "dots-underscores",
    "space", "single-quote", "semicolon", "dollar", "backtick",
    "ampersand", "pipe", "parens",
])
def test_shell_quote(input_str: str, expected: str) -> None:
    assert _shell_quote(input_str) == expected


def test_shell_quote_empty() -> None:
    # Empty string passes the all() check vacuously, so it's returned as-is
    result = _shell_quote("")
    assert result == ""


# ---------------------------------------------------------------------------
# collect_files
# ---------------------------------------------------------------------------

class TestCollectFiles:
    def test_basic(self, sample_dir: Path) -> None:
        cfg = Config()
        files = collect_files(cfg, str(sample_dir), [])
        assert "startup.nsh" in files
        assert "tools/readme.txt" in files
        assert "EFI/BOOT/BOOTX64.EFI" in files
        assert len(files) == 3

    def test_nested_paths_use_forward_slashes(self, sample_dir: Path) -> None:
        cfg = Config()
        files = collect_files(cfg, str(sample_dir), [])
        for key in files:
            assert "\\" not in key

    def test_empty_dir(self, tmp_path: Path) -> None:
        cfg = Config()
        files = collect_files(cfg, str(tmp_path), [])
        assert files == {}

    def test_nonexistent_dir(self) -> None:
        cfg = Config()
        with pytest.raises(FileNotFoundError):
            collect_files(cfg, "/nonexistent/path", [])

    def test_include_file(self, sample_dir: Path, tmp_path: Path) -> None:
        extra = tmp_path / "extra.txt"
        extra.write_text("extra\n")
        cfg = Config()
        files = collect_files(cfg, str(sample_dir), [str(extra)])
        assert "extra.txt" in files

    def test_include_dir(self, sample_dir: Path, tmp_path: Path) -> None:
        inc_dir = tmp_path / "inc"
        inc_dir.mkdir()
        (inc_dir / "a.txt").write_text("a\n")
        (inc_dir / "b.txt").write_text("b\n")
        cfg = Config()
        files = collect_files(cfg, str(sample_dir), [str(inc_dir)])
        assert "a.txt" in files
        assert "b.txt" in files

    def test_include_missing_warns(self, sample_dir: Path) -> None:
        messages: list[str] = []
        cfg = Config(log=lambda msg: messages.append(msg))
        files = collect_files(cfg, str(sample_dir), ["/nonexistent/file.txt"])
        # Should warn but not raise
        assert len(files) == 3  # only sample_dir files
        assert any("not found" in m for m in messages)

    def test_values_are_absolute_paths(self, sample_dir: Path) -> None:
        cfg = Config()
        files = collect_files(cfg, str(sample_dir), [])
        for local_path in files.values():
            assert os.path.isabs(local_path)


# ---------------------------------------------------------------------------
# _resolve
# ---------------------------------------------------------------------------

class TestResolve:
    def test_absolute_path(self, tmp_path: Path) -> None:
        result = _resolve(str(tmp_path))
        assert os.path.isabs(result)
        assert result == str(tmp_path.resolve())

    def test_relative_path(self) -> None:
        result = _resolve(".")
        assert os.path.isabs(result)

    def test_symlink_resolved(self, tmp_path: Path) -> None:
        real = tmp_path / "real.txt"
        real.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        result = _resolve(str(link))
        assert "real.txt" in result


# ---------------------------------------------------------------------------
# _parse_size
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("input_str,expected", [
    ("", 0),
    ("1024", 1024),
    ("512M", 512),
    ("512m", 512),
    ("4G", 4096),
    ("4g", 4096),
    ("1G", 1024),
], ids=["empty", "plain-mb", "megabytes", "megabytes-lower", "gigabytes",
        "gigabytes-lower", "1g"])
def test_parse_size(input_str: str, expected: int) -> None:
    assert _parse_size(input_str) == expected


def test_parse_size_invalid() -> None:
    with pytest.raises(ValueError):
        _parse_size("abc")


# ---------------------------------------------------------------------------
# _calculate_content_size
# ---------------------------------------------------------------------------

class TestCalculateContentSize:
    def test_small_files(self, sample_files: dict[str, str]) -> None:
        mb = _calculate_content_size(sample_files)
        assert mb >= 1

    def test_empty(self) -> None:
        assert _calculate_content_size({}) == 1  # minimum


# ---------------------------------------------------------------------------
# _stage_files
# ---------------------------------------------------------------------------

class TestStageFiles:
    def test_preserves_structure(self, sample_files: dict[str, str],
                                 tmp_path: Path) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        _stage_files(sample_files, staging)
        assert (staging / "startup.nsh").exists()
        assert (staging / "EFI" / "BOOT" / "BOOTX64.EFI").exists()
        assert (staging / "tools" / "readme.txt").exists()

    def test_file_content_preserved(self, sample_files: dict[str, str],
                                     tmp_path: Path) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        _stage_files(sample_files, staging)
        assert (staging / "startup.nsh").read_text() == "echo Hello\n"
