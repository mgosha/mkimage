"""macOS tests — run via SSH to macOS host.

All tests are marked @pytest.mark.macos and skip if the host is unreachable.
Requires: brew install mtools dosfstools gptfdisk xorriso on the Mac.
"""
from __future__ import annotations

import pytest

from conftest import MACOS_HOST, MKIMAGE_PY, MKIMAGE_GUI, macos_scp_to, macos_ssh

pytestmark = pytest.mark.macos

REMOTE_DIR = "~/mkimage"


@pytest.fixture(autouse=True)
def sync_files() -> None:
    """Ensure mkimage files are synced to the Mac before each test."""
    r1 = macos_scp_to(MKIMAGE_PY, f"{REMOTE_DIR}/mkimage.py")
    r2 = macos_scp_to(MKIMAGE_GUI, f"{REMOTE_DIR}/mkimage_gui.py")
    assert r1.returncode == 0, f"Failed to SCP mkimage.py: {r1.stderr}"
    assert r2.returncode == 0, f"Failed to SCP mkimage_gui.py: {r2.stderr}"


def _mkimage(args: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run mkimage.py on the Mac. Returns (exit_code, stdout, stderr)."""
    r = macos_ssh(f"cd {REMOTE_DIR} && python3 mkimage.py {args}", timeout=timeout)
    return r.returncode, r.stdout, r.stderr


class TestMacosCheck:
    def test_check_all_ok(self) -> None:
        rc, stdout, stderr = _mkimage("--check")
        assert rc == 0, f"--check failed:\n{stdout}\n{stderr}"
        assert "FAT32 (.img): OK" in stdout
        assert "ISO   (.iso): OK" in stdout
        assert "GPT   (.img): OK" in stdout

    def test_check_shows_native(self) -> None:
        rc, stdout, _ = _mkimage("--check")
        assert "native" in stdout


class TestMacosBuildImg:
    def test_fat32_image(self) -> None:
        # Create test files and build
        macos_ssh("mkdir -p /tmp/mkimage-mac-test && "
                  "echo hello > /tmp/mkimage-mac-test/hello.txt && "
                  "echo 'echo Test' > /tmp/mkimage-mac-test/startup.nsh")
        rc, stdout, stderr = _mkimage(
            "--source /tmp/mkimage-mac-test --target /tmp/mac-test.img",
            timeout=60,
        )
        assert rc == 0, f"Build failed:\n{stdout}\n{stderr}"
        assert "[OK]" in stdout
        assert "FAT32" in stdout

        # Verify the image exists and has correct type
        r = macos_ssh("file /tmp/mac-test.img")
        assert "FAT" in r.stdout or "filesystem" in r.stdout.lower()

        # Cleanup
        macos_ssh("rm -rf /tmp/mkimage-mac-test /tmp/mac-test.img")

    def test_custom_label(self) -> None:
        macos_ssh("mkdir -p /tmp/mkimage-mac-lbl && "
                  "echo test > /tmp/mkimage-mac-lbl/x.txt")
        rc, stdout, stderr = _mkimage(
            "--source /tmp/mkimage-mac-lbl --target /tmp/mac-lbl.img --label MACLABEL",
        )
        assert rc == 0, f"Build failed:\n{stdout}\n{stderr}"
        macos_ssh("rm -rf /tmp/mkimage-mac-lbl /tmp/mac-lbl.img")


class TestMacosBuildIso:
    def test_iso_image(self) -> None:
        macos_ssh("mkdir -p /tmp/mkimage-mac-iso && "
                  "echo hello > /tmp/mkimage-mac-iso/hello.txt")
        rc, stdout, stderr = _mkimage(
            "--source /tmp/mkimage-mac-iso --target /tmp/mac-test.iso",
        )
        assert rc == 0, f"Build failed:\n{stdout}\n{stderr}"
        assert "ISO 9660" in stdout

        r = macos_ssh("file /tmp/mac-test.iso")
        assert "ISO 9660" in r.stdout

        macos_ssh("rm -rf /tmp/mkimage-mac-iso /tmp/mac-test.iso")


class TestMacosGpt:
    def test_gpt_requires_root(self) -> None:
        """GPT without root should give a clear error."""
        macos_ssh("mkdir -p /tmp/mkimage-mac-gpt && "
                  "echo test > /tmp/mkimage-mac-gpt/x.txt")
        rc, stdout, stderr = _mkimage(
            "--source /tmp/mkimage-mac-gpt --target /tmp/mac-gpt.img --gpt",
        )
        assert rc != 0
        combined = stdout + stderr
        assert "root" in combined.lower() or "sudo" in combined.lower()
        assert "hdiutil" in combined  # macOS-specific message

        macos_ssh("rm -rf /tmp/mkimage-mac-gpt /tmp/mac-gpt.img")


class TestMacosListDrives:
    def test_list_drives_runs(self) -> None:
        rc, stdout, stderr = _mkimage("--list-drives")
        assert rc == 0
        # May or may not find drives, but should not error
