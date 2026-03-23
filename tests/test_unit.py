"""Pure unit tests — no root, no tools, no network."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mkimage import Config, _resolve, _shell_quote, collect_files


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
        assert cfg.extra_mb == 32
        assert cfg.force is False
        assert cfg.data_dir == ""
        assert cfg.data_size == ""
        assert cfg.esp_label == "ESP"
        assert cfg.data_label == "DATA"

    def test_custom_values(self) -> None:
        cfg = Config(verbose=True, label="MYTOOLS", extra_mb=64, gpt=True)
        assert cfg.verbose is True
        assert cfg.label == "MYTOOLS"
        assert cfg.extra_mb == 64
        assert cfg.gpt is True

    def test_log_default_is_print(self) -> None:
        cfg = Config()
        assert cfg.log is print

    def test_log_captures(self) -> None:
        messages: list[str] = []
        cfg = Config(log=lambda msg: messages.append(msg))
        cfg.log("hello")
        cfg.log("world")
        assert messages == ["hello", "world"]


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
