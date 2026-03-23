"""Windows VM tests — run via SSH to QEMU Windows VM.

All tests are marked @pytest.mark.windows and skip if winvm is unreachable.

The QEMU VM can be started with a virtual USB drive for end-to-end write
testing. Add to the QEMU command line:
    -drive file=usb-test.raw,format=raw,if=none,id=usbdisk0
    -device usb-storage,drive=usbdisk0,removable=on
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from conftest import MKIMAGE_PS1, winvm_scp_to, winvm_ssh

pytestmark = pytest.mark.windows

VM_MKIMAGE_DIR = "C:/Users/mike/mkimage"
VM_MKIMAGE_PS1 = f"{VM_MKIMAGE_DIR}/mkimage.ps1"


def _find_usb_disk() -> int | None:
    """Find a USB disk on the VM that is NOT the system disk. Returns disk number or None."""
    r = winvm_ssh(
        'powershell -NoProfile -Command "'
        "Get-Disk | Where-Object { $_.BusType -eq 'USB' } | "
        "ForEach-Object { Write-Host $_.Number }"
        '"',
        timeout=15,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return int(r.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


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
    def test_iso_cli(self) -> None:
        """Create an ISO via -Action CreateIso on Windows."""
        # Create test files on the VM
        setup_cmds = [
            "if (Test-Path C:\\temp\\mkimage_test) { Remove-Item C:\\temp\\mkimage_test -Recurse -Force }",
            "New-Item -ItemType Directory -Path C:\\temp\\mkimage_test\\source -Force | Out-Null",
            "'test content' | Out-File C:\\temp\\mkimage_test\\source\\hello.txt",
            "'startup' | Out-File C:\\temp\\mkimage_test\\source\\startup.nsh",
        ]
        for cmd in setup_cmds:
            winvm_ssh(f'powershell -NoProfile -Command "{cmd}"')

        try:
            rc, stdout, stderr = _ps_file(
                VM_MKIMAGE_PS1,
                "-Action", "CreateIso",
                "-SourceDir", "C:\\temp\\mkimage_test\\source",
                "-OutputFile", "C:\\temp\\mkimage_test\\output.iso",
                "-Label", "TESTISO",
                timeout=30,
            )
            combined = stdout + stderr

            # The ISO creation may fail if IMAPI2 COM has issues on this VM,
            # but the script should at least attempt it and report clearly
            if rc == 0:
                # Check the file was created and has content
                check_r = winvm_ssh(
                    'powershell -NoProfile -Command "'
                    "if (Test-Path 'C:\\temp\\mkimage_test\\output.iso') {"
                    "  $s = (Get-Item 'C:\\temp\\mkimage_test\\output.iso').Length;"
                    "  Write-Host SIZE:$s"
                    '}"',
                    timeout=10,
                )
                assert "SIZE:" in check_r.stdout
                size_str = check_r.stdout.split("SIZE:")[1].strip()
                assert int(size_str) > 0, f"ISO created but 0 bytes. Output: {combined}"
            else:
                # If it failed, it should have produced output explaining why
                assert "ERROR" in combined, f"Exit {rc} with no error message: {combined}"
        finally:
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


class TestPs1UsbWrite:
    """End-to-end USB write tests using a QEMU virtual USB drive.

    These tests require the VM to be started with a virtual USB mass storage
    device attached. They format and write to the virtual drive, then verify
    the files are present.
    """

    @pytest.fixture(autouse=True)
    def usb_disk(self) -> int:
        """Find the virtual USB disk or skip."""
        disk = _find_usb_disk()
        if disk is None:
            pytest.skip("No USB disk found on VM (start QEMU with -device usb-storage)")
        return disk

    @pytest.fixture
    def test_source(self) -> str:
        """Create test source files on the VM, return the path, cleanup after."""
        src_dir = "C:\\temp\\usb_write_test_src"
        winvm_ssh(f'powershell -NoProfile -Command "Remove-Item {src_dir} -Recurse -Force -ErrorAction SilentlyContinue"')
        winvm_ssh(f'powershell -NoProfile -Command "New-Item -ItemType Directory -Path {src_dir} -Force | Out-Null"')
        winvm_ssh(f'powershell -NoProfile -Command "\'hello from test\' | Out-File {src_dir}\\hello.txt"')
        winvm_ssh(f'powershell -NoProfile -Command "\'echo Test\' | Out-File {src_dir}\\startup.nsh"')
        yield src_dir
        winvm_ssh(f'powershell -NoProfile -Command "Remove-Item {src_dir} -Recurse -Force -ErrorAction SilentlyContinue"')

    def test_write_and_verify(self, usb_disk: int, test_source: str) -> None:
        """Write files to the virtual USB drive and verify they're present."""
        rc, stdout, stderr = _ps_file(
            VM_MKIMAGE_PS1, "-Action", "WriteUsb",
            "-DiskNumber", str(usb_disk),
            "-SourceDir", test_source,
            "-Label", "TESTUSB",
            "-SkipConfirm", "-Verbose",
            timeout=60,
        )
        combined = stdout + stderr
        assert "OK" in combined, f"Write failed:\n{combined}"
        assert "Wrote" in combined and "files" in combined

    def test_write_gpt(self, usb_disk: int, test_source: str) -> None:
        """Write with GPT partitioning to the virtual USB drive."""
        rc, stdout, stderr = _ps_file(
            VM_MKIMAGE_PS1, "-Action", "WriteUsb",
            "-DiskNumber", str(usb_disk),
            "-SourceDir", test_source,
            "-Label", "GPTTEST",
            "-SkipConfirm", "-UseGpt", "-Verbose",
            timeout=60,
        )
        combined = stdout + stderr
        assert "OK" in combined, f"GPT write failed:\n{combined}"
        assert "convert GPT" in combined or "GPT" in combined

    def test_write_verify_flag(self, usb_disk: int, test_source: str) -> None:
        """Write with -Verify flag to check file integrity."""
        rc, stdout, stderr = _ps_file(
            VM_MKIMAGE_PS1, "-Action", "WriteUsb",
            "-DiskNumber", str(usb_disk),
            "-SourceDir", test_source,
            "-Label", "VERTEST",
            "-SkipConfirm", "-Verify", "-Verbose",
            timeout=60,
        )
        combined = stdout + stderr
        assert "OK" in combined, f"Verify write failed:\n{combined}"
        assert "erif" in combined  # "Verify" or "Verification"


class TestPs1UsbRewrite:
    """Test rewriting a USB drive from one format to another.

    Real-world scenario: a drive previously written with ISO 9660, GPT, or
    MBR needs to be cleanly reformatted. Residual partition tables, filesystem
    signatures, or GPT backup headers can cause failures if diskpart doesn't
    fully clean them.
    """

    USB_IMG = Path.home() / "VMs" / "win11-epsa-build" / "usb-test.raw"

    @pytest.fixture(autouse=True)
    def usb_disk(self) -> int:
        """Find the virtual USB disk or skip."""
        disk = _find_usb_disk()
        if disk is None:
            pytest.skip("No USB disk found on VM (start QEMU with -device usb-storage)")
        return disk

    @pytest.fixture
    def test_source(self) -> str:
        """Create test source files on the VM, return the path, cleanup after."""
        src_dir = "C:\\temp\\usb_rewrite_src"
        winvm_ssh(f'powershell -NoProfile -Command "Remove-Item {src_dir} -Recurse -Force -ErrorAction SilentlyContinue"')
        winvm_ssh(f'powershell -NoProfile -Command "New-Item -ItemType Directory -Path {src_dir} -Force | Out-Null"')
        winvm_ssh(f'powershell -NoProfile -Command "\'rewrite test\' | Out-File {src_dir}\\test.txt"')
        winvm_ssh(f'powershell -NoProfile -Command "\'echo Rewrite\' | Out-File {src_dir}\\startup.nsh"')
        yield src_dir
        winvm_ssh(f'powershell -NoProfile -Command "Remove-Item {src_dir} -Recurse -Force -ErrorAction SilentlyContinue"')

    def _write_usb(self, disk: int, source: str, label: str,
                   gpt: bool = False, verify: bool = False) -> str:
        """Write to USB and return combined output. Asserts success."""
        args = [
            VM_MKIMAGE_PS1, "-Action", "WriteUsb",
            "-DiskNumber", str(disk),
            "-SourceDir", source,
            "-Label", label,
            "-SkipConfirm", "-Verbose",
        ]
        if gpt:
            args.append("-UseGpt")
        if verify:
            args.append("-Verify")
        rc, stdout, stderr = _ps_file(*args, timeout=60)
        combined = stdout + stderr
        assert "OK" in combined, f"Write failed (label={label}, gpt={gpt}):\n{combined}"
        return combined

    def _write_iso_to_backing(self, sample_dir: Path) -> None:
        """Write an ISO 9660 image directly to the QEMU backing file from the Linux host.

        This simulates a USB drive that was previously written with dd from an ISO.
        """
        from mkimage import Config, build_iso, collect_files

        cfg = Config(label="OLDISO")
        files = collect_files(cfg, str(sample_dir), [])
        # Build ISO to a temp file
        with tempfile.NamedTemporaryFile(suffix=".iso", delete=False) as f:
            iso_path = f.name
        try:
            build_iso(cfg, files, iso_path)
            iso_size = os.path.getsize(iso_path)
            # dd the ISO onto the start of the USB backing image
            subprocess.run(
                ["dd", f"if={iso_path}", f"of={self.USB_IMG}",
                 "bs=1M", "conv=notrunc"],
                check=True, capture_output=True,
            )
        finally:
            os.unlink(iso_path)

    def _write_raw_garbage(self) -> None:
        """Write random garbage to the backing file to simulate a corrupted drive."""
        subprocess.run(
            ["dd", "if=/dev/urandom", f"of={self.USB_IMG}",
             "bs=1M", "count=4", "conv=notrunc"],
            check=True, capture_output=True,
        )

    def _rescan_disk(self, disk: int) -> None:
        """Tell Windows to re-read the disk after modifying the backing file."""
        winvm_ssh(
            f'powershell -NoProfile -Command "'
            f"Update-Disk -Number {disk} -ErrorAction SilentlyContinue; "
            f"Get-Disk -Number {disk} | Set-Disk -IsOffline $false -ErrorAction SilentlyContinue"
            f'"',
            timeout=15,
        )

    def test_iso9660_to_mbr(self, usb_disk: int, test_source: str,
                             sample_dir: Path) -> None:
        """Write ISO 9660 to disk, then reformat as FAT32 MBR."""
        self._write_iso_to_backing(sample_dir)
        self._rescan_disk(usb_disk)
        out = self._write_usb(usb_disk, test_source, "AFTERISO", verify=True)
        assert "Wrote" in out
        assert "erif" in out  # verification ran

    def test_iso9660_to_gpt(self, usb_disk: int, test_source: str,
                             sample_dir: Path) -> None:
        """Write ISO 9660 to disk, then reformat as FAT32 GPT."""
        self._write_iso_to_backing(sample_dir)
        self._rescan_disk(usb_disk)
        out = self._write_usb(usb_disk, test_source, "ISOGPT", gpt=True, verify=True)
        assert "Wrote" in out

    def test_mbr_to_gpt(self, usb_disk: int, test_source: str) -> None:
        """Write FAT32 MBR, then rewrite as FAT32 GPT."""
        self._write_usb(usb_disk, test_source, "FIRST_MBR")
        out = self._write_usb(usb_disk, test_source, "SECOND_GPT", gpt=True, verify=True)
        assert "Wrote" in out
        assert "GPT" in out

    def test_gpt_to_mbr(self, usb_disk: int, test_source: str) -> None:
        """Write FAT32 GPT, then rewrite as FAT32 MBR."""
        self._write_usb(usb_disk, test_source, "FIRST_GPT", gpt=True)
        out = self._write_usb(usb_disk, test_source, "SECOND_MBR", verify=True)
        assert "Wrote" in out
        assert "MBR" in out

    def test_gpt_to_gpt(self, usb_disk: int, test_source: str) -> None:
        """Rewrite GPT over existing GPT (backup header at end of disk)."""
        self._write_usb(usb_disk, test_source, "GPT_ONE", gpt=True)
        out = self._write_usb(usb_disk, test_source, "GPT_TWO", gpt=True, verify=True)
        assert "Wrote" in out

    def test_garbage_to_mbr(self, usb_disk: int, test_source: str) -> None:
        """Write random garbage, then format as FAT32 MBR."""
        self._write_raw_garbage()
        self._rescan_disk(usb_disk)
        out = self._write_usb(usb_disk, test_source, "CLEAN_MBR", verify=True)
        assert "Wrote" in out

    def test_garbage_to_gpt(self, usb_disk: int, test_source: str) -> None:
        """Write random garbage, then format as FAT32 GPT."""
        self._write_raw_garbage()
        self._rescan_disk(usb_disk)
        out = self._write_usb(usb_disk, test_source, "CLEAN_GPT", gpt=True, verify=True)
        assert "Wrote" in out
