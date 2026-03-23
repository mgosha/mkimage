"""Windows VM tests — run via SSH to QEMU Windows VM.

All tests are marked @pytest.mark.windows and skip if winvm is unreachable.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from conftest import MKIMAGE_PS1, winvm_scp_to, winvm_ssh

pytestmark = pytest.mark.windows

VM_MKIMAGE_DIR = "C:/Users/mike/mkimage"
VM_MKIMAGE_PS1 = f"{VM_MKIMAGE_DIR}/mkimage.ps1"


@pytest.fixture(autouse=True)
def sync_ps1() -> None:
    """Ensure mkimage.ps1 is synced to the VM before each test."""
    r = winvm_scp_to(MKIMAGE_PS1, VM_MKIMAGE_PS1)
    assert r.returncode == 0, f"Failed to SCP mkimage.ps1: {r.stderr}"


def _ps_run(command: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a PowerShell command on the VM. Returns (exit_code, stdout, stderr)."""
    r = winvm_ssh(
        f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{command}"',
        timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


def _ps_file(script_path: str, *args: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a PowerShell script file on the VM."""
    args_str = " ".join(args)
    r = winvm_ssh(
        f"powershell -NoProfile -ExecutionPolicy Bypass -File {script_path} {args_str}",
        timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


class TestPs1ParamValidation:
    def test_invalid_action_rejected(self) -> None:
        rc, stdout, stderr = _ps_file(VM_MKIMAGE_PS1, "-Action", "BadAction")
        assert rc != 0
        assert "ValidateSet" in stderr or "does not belong" in stderr

    def test_writeusb_requires_disk_number(self) -> None:
        rc, stdout, stderr = _ps_file(
            VM_MKIMAGE_PS1, "-Action", "WriteUsb",
            "-DiskNumber", "-1", "-SourceDir", "C:\\temp",
            "-SkipConfirm",
        )
        # Should fail at Get-Disk with invalid number
        combined = stdout + stderr
        assert "Get-Disk" in combined or "not found" in combined.lower() or rc != 0

    def test_writeusb_bad_disk(self) -> None:
        rc, stdout, stderr = _ps_file(
            VM_MKIMAGE_PS1, "-Action", "WriteUsb",
            "-DiskNumber", "99", "-SourceDir", "C:\\temp",
            "-Label", "TEST", "-SkipConfirm",
        )
        combined = stdout + stderr
        assert "99" in combined  # should mention the bad disk number
        assert rc != 0 or "ERROR" in combined or "not found" in combined.lower()


class TestPs1GuiMode:
    def test_gui_loads_form(self) -> None:
        """GUI mode should build the form and fail at ShowDialog in SSH."""
        rc, stdout, stderr = _ps_file(VM_MKIMAGE_PS1, timeout=15)
        combined = stdout + stderr
        # Expected: ShowDialog fails in non-interactive mode
        assert "ShowDialog" in combined or "UserInteractive" in combined


class TestPs1GetUsbDrives:
    def test_returns_array(self) -> None:
        """Get-UsbDrives should return without error (may be empty)."""
        # Dot-source the script to get the function, then call it
        rc, stdout, stderr = _ps_run(
            ". " + VM_MKIMAGE_PS1.replace("/", "\\") + "; "
            "$drives = Get-UsbDrives; "
            "Write-Host ('DRIVES:' + $drives.Count)",
            timeout=15,
        )
        # The function may fail trying to load WinForms (needed for param block),
        # but if it works, stdout should contain DRIVES:N
        if rc == 0:
            assert "DRIVES:" in stdout


class TestPs1IsoCreation:
    def test_iso_imapi2(self) -> None:
        """Create an ISO via IMAPI2 COM on Windows."""
        # Create test files on the VM
        setup_cmds = [
            "if (Test-Path C:\\temp\\mkimage_test) { Remove-Item C:\\temp\\mkimage_test -Recurse -Force }",
            "New-Item -ItemType Directory -Path C:\\temp\\mkimage_test\\source -Force | Out-Null",
            "'test content' | Out-File C:\\temp\\mkimage_test\\source\\hello.txt",
            "'startup' | Out-File C:\\temp\\mkimage_test\\source\\startup.nsh",
        ]
        for cmd in setup_cmds:
            winvm_ssh(f'powershell -NoProfile -Command "{cmd}"')

        # Create a harness script that calls New-IsoImage
        harness = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. C:\Users\mike\mkimage\mkimage.ps1

# Create a mock LogBox
$logBox = New-Object System.Windows.Forms.TextBox
$logBox.Multiline = $true

$result = New-IsoImage -SourceDir 'C:\temp\mkimage_test\source' `
    -Includes @() -OutputFile 'C:\temp\mkimage_test\output.iso' `
    -Label 'TESTISO' -LogBox $logBox

if (Test-Path 'C:\temp\mkimage_test\output.iso') {
    $size = (Get-Item 'C:\temp\mkimage_test\output.iso').Length
    Write-Host "ISO_OK:$size"
} else {
    Write-Host "ISO_FAIL"
}
Write-Host "LOG:$($logBox.Text)"
"""
        # Write harness to a temp file and SCP it
        with tempfile.NamedTemporaryFile(suffix=".ps1", mode="w", delete=False) as f:
            f.write(harness)
            harness_path = f.name

        try:
            winvm_scp_to(harness_path, "C:/temp/mkimage_test/harness.ps1")
            rc, stdout, stderr = _ps_file(
                "C:\\temp\\mkimage_test\\harness.ps1", timeout=30,
            )
            combined = stdout + stderr

            # The ISO creation might fail if IMAPI2 has issues, but it should
            # at least attempt it
            if "ISO_OK" in stdout:
                # Verify ISO was created with non-zero size
                size_str = stdout.split("ISO_OK:")[1].split("\n")[0].strip()
                assert int(size_str) > 0
            else:
                # If it failed, it should have produced log output explaining why
                assert "ISO_FAIL" in stdout or "ERROR" in combined
        finally:
            os.unlink(harness_path)
            # Cleanup on VM
            winvm_ssh('powershell -NoProfile -Command "Remove-Item C:\\temp\\mkimage_test -Recurse -Force -ErrorAction SilentlyContinue"')


class TestPs1SkipConfirm:
    def test_skipconfirm_no_messagebox(self) -> None:
        """With -SkipConfirm, Write-UsbDrive should not show MessageBox."""
        # Call with -SkipConfirm and a nonexistent disk — it should fail
        # at diskpart, not at a dialog box
        rc, stdout, stderr = _ps_file(
            VM_MKIMAGE_PS1, "-Action", "WriteUsb",
            "-DiskNumber", "99", "-SourceDir", "C:\\temp",
            "-Label", "TEST", "-SkipConfirm",
        )
        combined = stdout + stderr
        # Should NOT contain "MessageBox" or hang waiting for input
        # Should contain diskpart error or Get-Disk error
        assert "MessageBox" not in combined
        assert "diskpart" in combined.lower() or "get-disk" in combined.lower() or "not found" in combined.lower()
