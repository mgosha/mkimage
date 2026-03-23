"""Shared fixtures, markers, and helpers for mkimage tests."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Add project root to path so we can import mkimage
sys.path.insert(0, str(Path(__file__).parent.parent))

from mkimage import Config, collect_files

MKIMAGE_PY = str(Path(__file__).parent.parent / "mkimage.py")
MKIMAGE_GUI = str(Path(__file__).parent.parent / "mkimage_gui.py")
MKIMAGE_PS1 = str(Path(__file__).parent.parent / "mkimage.ps1")


# ---------------------------------------------------------------------------
# Auto-skip logic for markers
# ---------------------------------------------------------------------------

def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-skip tests based on markers."""
    for item in items:
        if "needs_root" in item.keywords and os.geteuid() != 0:
            item.add_marker(pytest.mark.skip(reason="requires root"))
        if "windows" in item.keywords and not _winvm_reachable():
            item.add_marker(pytest.mark.skip(reason="Windows VM (winvm) not reachable"))
        if "macos" in item.keywords and not _macos_reachable():
            item.add_marker(pytest.mark.skip(reason="macOS host not reachable"))


_winvm_ok: bool | None = None


def _winvm_reachable() -> bool:
    """Check SSH connectivity to winvm (cached for session)."""
    global _winvm_ok
    if _winvm_ok is not None:
        return _winvm_ok
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes",
             "winvm", "echo", "ok"],
            capture_output=True, text=True, timeout=10,
        )
        _winvm_ok = r.returncode == 0 and "ok" in r.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        _winvm_ok = False
    return _winvm_ok


MACOS_HOST = "100.108.244.116"
_macos_ok: bool | None = None


def _macos_reachable() -> bool:
    """Check SSH connectivity to macOS host (cached for session)."""
    global _macos_ok
    if _macos_ok is not None:
        return _macos_ok
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes",
             MACOS_HOST, "echo", "ok"],
            capture_output=True, text=True, timeout=10,
        )
        _macos_ok = r.returncode == 0 and "ok" in r.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        _macos_ok = False
    return _macos_ok


def macos_ssh(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run a command on the macOS host via SSH."""
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
         MACOS_HOST, cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def macos_scp_to(local_path: str, remote_path: str) -> subprocess.CompletedProcess[str]:
    """SCP a file to the macOS host."""
    return subprocess.run(
        ["scp", "-o", "BatchMode=yes", local_path,
         f"{MACOS_HOST}:{remote_path}"],
        capture_output=True, text=True, timeout=30,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_dir(tmp_path: Path) -> Path:
    """Create a temporary directory with a realistic UEFI file tree."""
    # EFI/BOOT/BOOTX64.EFI — small binary
    efi_dir = tmp_path / "EFI" / "BOOT"
    efi_dir.mkdir(parents=True)
    (efi_dir / "BOOTX64.EFI").write_bytes(b"\x90" * 1024)

    # startup.nsh — text file
    (tmp_path / "startup.nsh").write_text("echo Hello\n")

    # tools/readme.txt — nested text
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "readme.txt").write_text("test content\n")

    return tmp_path


@pytest.fixture
def sample_files(sample_dir: Path) -> dict[str, str]:
    """Collect files from the sample directory."""
    cfg = Config()
    return collect_files(cfg, str(sample_dir), [])


@pytest.fixture
def cfg() -> tuple[Config, list[str]]:
    """Config with a capturing log function."""
    messages: list[str] = []
    config = Config(log=lambda msg: messages.append(msg))
    return config, messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_mkimage(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run mkimage.py as a subprocess."""
    return subprocess.run(
        [sys.executable, MKIMAGE_PY, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def winvm_ssh(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run a command on the Windows VM via SSH."""
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
         "winvm", cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def winvm_scp_to(local_path: str, remote_path: str) -> subprocess.CompletedProcess[str]:
    """SCP a file to the Windows VM."""
    return subprocess.run(
        ["scp", "-o", "BatchMode=yes", local_path,
         f"winvm:{remote_path}"],
        capture_output=True, text=True, timeout=30,
    )


def winvm_scp_from(remote_path: str, local_path: str) -> subprocess.CompletedProcess[str]:
    """SCP a file from the Windows VM."""
    return subprocess.run(
        ["scp", "-o", "BatchMode=yes",
         f"winvm:{remote_path}", local_path],
        capture_output=True, text=True, timeout=30,
    )
