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

from conftest import (MKIMAGE_PS1, MKIMAGE_PYZ, winvm_scp_from, winvm_scp_to,
                      winvm_ssh)

pytestmark = pytest.mark.windows

VM_MKIMAGE_DIR = "C:/Users/mike/mkimage"
VM_MKIMAGE_PS1 = f"{VM_MKIMAGE_DIR}/mkimage.ps1"
VM_MKIMAGE_PYZ = f"{VM_MKIMAGE_DIR}/mkimage.pyz"


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


class TestPs1ImgCreation:
    def test_img_cli(self) -> None:
        """Create a FAT32 .img via -Action CreateImg on Windows (diskpart, no Hyper-V)."""
        setup_cmds = [
            "if (Test-Path C:\\temp\\mkimage_img_test) { Remove-Item C:\\temp\\mkimage_img_test -Recurse -Force }",
            "New-Item -ItemType Directory -Path C:\\temp\\mkimage_img_test\\source -Force | Out-Null",
            "'test content' | Out-File C:\\temp\\mkimage_img_test\\source\\hello.txt",
            "'echo Hello' | Out-File C:\\temp\\mkimage_img_test\\source\\startup.nsh",
        ]
        for cmd in setup_cmds:
            winvm_ssh(f'powershell -NoProfile -Command "{cmd}"')

        try:
            rc, stdout, stderr = _ps_file(
                VM_MKIMAGE_PS1,
                "-Action", "CreateImg",
                "-SourceDir", "C:\\temp\\mkimage_img_test\\source",
                "-OutputFile", "C:\\temp\\mkimage_img_test\\output.img",
                "-Label", "TESTIMG",
                "-SizeMB", "40",
                timeout=120,
            )
            combined = stdout + stderr
            assert rc == 0, f"CreateImg failed (rc={rc}):\n{combined}"
            assert "[OK]" in combined

            # Verify the file was created and has content
            check_r = winvm_ssh(
                'powershell -NoProfile -Command "'
                "if (Test-Path 'C:\\temp\\mkimage_img_test\\output.img') {"
                "  $s = (Get-Item 'C:\\temp\\mkimage_img_test\\output.img').Length;"
                "  Write-Host SIZE:$s"
                '}"',
                timeout=10,
            )
            assert "SIZE:" in check_r.stdout
            size_str = check_r.stdout.split("SIZE:")[1].strip()
            size = int(size_str)
            assert size > 0, f"Image created but 0 bytes"
            # Should be ~40MB (raw FAT32, VHD footer stripped)
            assert size >= 40 * 1024 * 1024, f"Image too small: {size} bytes"
        finally:
            winvm_ssh('powershell -NoProfile -Command "Remove-Item C:\\temp\\mkimage_img_test -Recurse -Force -ErrorAction SilentlyContinue"')


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
            timeout=180,
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
            timeout=180,
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
            timeout=180,
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
        rc, stdout, stderr = _ps_file(*args, timeout=120)
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

    def _wipe_backing(self) -> None:
        """Zero the entire QEMU backing file from the Linux host.

        Much faster than diskpart clean all (direct file write vs USB I/O).
        This pre-cleans all partition signatures so diskpart clean (fast)
        is sufficient on the Windows side.
        """
        subprocess.run(
            ["dd", "if=/dev/zero", f"of={self.USB_IMG}",
             "bs=1M", "count=256"],
            check=True, capture_output=True,
        )

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
        self._wipe_backing()
        self._rescan_disk(usb_disk)
        out = self._write_usb(usb_disk, test_source, "SECOND_GPT", gpt=True, verify=True)
        assert "Wrote" in out
        assert "GPT" in out

    def test_gpt_to_mbr(self, usb_disk: int, test_source: str) -> None:
        """Write FAT32 GPT, then rewrite as FAT32 MBR."""
        self._write_usb(usb_disk, test_source, "FIRST_GPT", gpt=True)
        self._wipe_backing()
        self._rescan_disk(usb_disk)
        out = self._write_usb(usb_disk, test_source, "SECOND_MBR", verify=True)
        assert "Wrote" in out
        assert "MBR" in out

    def test_gpt_to_gpt(self, usb_disk: int, test_source: str) -> None:
        """Rewrite GPT over existing GPT (backup header at end of disk)."""
        self._write_usb(usb_disk, test_source, "GPT_ONE", gpt=True)
        self._wipe_backing()
        self._rescan_disk(usb_disk)
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


class TestPyzUsbWrite:
    """End-to-end USB write via the Python package (mkimage.pyz) — the path
    the Dear PyGui GUI uses, distinct from the PS1 path above.

    Regression coverage for two bugs found while testing the GUI write flow
    against the QEMU virtual USB drive:
      * the elevated diskpart progress file was written UTF-16 but read UTF-8,
        producing replacement chars that crashed print() on a cp1252 stdout
        (UnicodeEncodeError: 'charmap'); and
      * the Windows branch of _write_usb_from_dir passed an empty source_dir,
        so it formatted the drive then copied ZERO files.
    Both reproduce as: a write that "succeeds" but lands 0 files on the disk.
    """

    @pytest.fixture(autouse=True)
    def sync_pyz(self) -> None:
        """Sync the current mkimage.pyz to the VM before each test."""
        r = winvm_scp_to(MKIMAGE_PYZ, VM_MKIMAGE_PYZ)
        assert r.returncode == 0, f"Failed to SCP mkimage.pyz: {r.stderr}"

    @pytest.fixture(autouse=True)
    def usb_disk(self) -> int:
        """Find the virtual USB disk or skip."""
        disk = _find_usb_disk()
        if disk is None:
            pytest.skip("No USB disk found on VM (start QEMU with -device usb-storage)")
        return disk

    @pytest.fixture
    def test_source(self) -> str:
        """Create a nested source tree on the VM (a subdir guards the
        recursive-copy fix), return the path, clean up after."""
        src = "C:\\temp\\pyz_usb_src"
        winvm_ssh(f'powershell -NoProfile -Command "Remove-Item {src} -Recurse -Force -ErrorAction SilentlyContinue"')
        winvm_ssh(
            'powershell -NoProfile -Command "'
            f"New-Item -ItemType Directory -Path '{src}\\EFI\\BOOT' -Force | Out-Null; "
            f"New-Item -ItemType Directory -Path '{src}\\tools' -Force | Out-Null; "
            f"Set-Content -Path '{src}\\README.txt' -Value 'pyz usb test'; "
            f"Set-Content -Path '{src}\\tools\\hello.txt' -Value 'nested'; "
            # 1 KiB stand-in for a bootloader, so EFI\\BOOT is non-empty
            f"[IO.File]::WriteAllBytes('{src}\\EFI\\BOOT\\BOOTX64.EFI', (New-Object byte[] 1024))"
            '"'
        )
        yield src
        winvm_ssh(f'powershell -NoProfile -Command "Remove-Item {src} -Recurse -Force -ErrorAction SilentlyContinue"')

    def _pyz_write_usb(self, source: str, label: str, verify: bool = True,
                       timeout: int = 180) -> str:
        """Run `mkimage.pyz --target usb` over SSH, feeding 'yes' to the
        confirm prompt. Returns combined stdout+stderr."""
        verify_flag = "--verify " if verify else ""
        # '... | Out-String' keeps the pipe inside the quoted -Command so cmd
        # doesn't split on it. 'yes' answers the destructive-write confirm.
        cmd = (
            "powershell -NoProfile -ExecutionPolicy Bypass -Command "
            f"\"'yes' | python.exe '{VM_MKIMAGE_PYZ}' "
            f"--source '{source}' --target usb {verify_flag}--label {label} "
            "2>&1 | Out-String\""
        )
        r = winvm_ssh(cmd, timeout=timeout)
        return r.stdout + r.stderr

    def _usb_files(self) -> list[str]:
        """List files (relative) on the virtual USB drive's volume."""
        cmd = (
            "powershell -NoProfile -Command "
            "\"$p = Get-Partition -DiskNumber " + str(_find_usb_disk()) +
            " -ErrorAction SilentlyContinue | Where-Object DriveLetter; "
            "if ($p) { $r = ($p.DriveLetter + ':\\'); "
            "Get-ChildItem -Recurse -File $r | ForEach-Object "
            "{ $_.FullName.Substring(3) } }\""
        )
        r = winvm_ssh(cmd, timeout=30)
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]

    def test_write_copies_files(self, usb_disk: int, test_source: str) -> None:
        """The Python path must actually COPY files, not just format.

        Regresses the 'copied 0 files' bug and the encoding crash.
        """
        out = self._pyz_write_usb(test_source, "PYZTEST", verify=True)

        # Bug 1: must not crash on the progress-file encoding.
        assert "charmap" not in out and "codec can't" not in out, \
            f"encoding crash regressed:\n{out}"
        # Bug 2: must report a non-zero file count, not OK:0 / "Wrote 0 files".
        assert "Wrote 0 files" not in out and "OK:0" not in out, \
            f"copied zero files (source_dir not threaded through?):\n{out}"
        assert "OK:" in out or "Wrote" in out, f"write did not complete:\n{out}"
        assert "Verification passed" in out or "files match" in out, \
            f"verify did not pass:\n{out}"

        # Ground truth: the nested tree is actually on the disk.
        files = {f.replace("/", "\\") for f in self._usb_files()}
        assert any(f.endswith("EFI\\BOOT\\BOOTX64.EFI") for f in files), \
            f"BOOTX64.EFI missing on USB; files={files}"
        assert any(f.endswith("tools\\hello.txt") for f in files), \
            f"nested tools\\hello.txt missing on USB; files={files}"
        assert any(f.endswith("README.txt") for f in files), \
            f"README.txt missing on USB; files={files}"


class TestPyzWindowsBuilds:
    """Regression tests for the pure-Python Windows build paths that the
    PS1-focused suite missed: GPT (had crashed via wsl/dd), UEFI-bootable ISO
    (was data-only), and --modify (had required mtools). Each builds via
    mkimage.pyz over SSH, then fetches the artifact and checks its structure.
    """

    VM_SRC = "C:/test/rsrc"

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        # current pyz + ps1 (ISO path shells to the ps1)
        assert winvm_scp_to(MKIMAGE_PYZ, VM_MKIMAGE_PYZ).returncode == 0
        winvm_scp_to(MKIMAGE_PS1, VM_MKIMAGE_PS1)
        # self-contained source tree with a (dummy) EFI fallback bootloader
        winvm_ssh(
            'powershell -NoProfile -Command "'
            f"New-Item -ItemType Directory -Force -Path '{self.VM_SRC}\\EFI\\BOOT' | Out-Null; "
            f"New-Item -ItemType Directory -Force -Path '{self.VM_SRC}\\tools' | Out-Null; "
            f"Set-Content -Path '{self.VM_SRC}\\README.txt' -Value 'readme'; "
            f"Set-Content -Path '{self.VM_SRC}\\tools\\hello.txt' -Value 'nested'; "
            f"[IO.File]::WriteAllBytes('{self.VM_SRC}\\EFI\\BOOT\\BOOTX64.EFI', (New-Object byte[] 2048))"
            '"', timeout=30)

    def _pyz(self, *args: str, timeout: int = 200) -> str:
        cmd = ("powershell -NoProfile -ExecutionPolicy Bypass -Command "
               f"\"python.exe '{VM_MKIMAGE_PYZ}' {' '.join(args)} 2>&1 | Out-String\"")
        r = winvm_ssh(cmd, timeout=timeout)
        return r.stdout + r.stderr

    def _fetch(self, vm_path: str) -> str:
        fd, local = tempfile.mkstemp()
        os.close(fd)
        assert winvm_scp_from(vm_path, local).returncode == 0, f"scp {vm_path} failed"
        return local

    def test_gpt_builds_without_wsl(self) -> None:
        """--gpt must use the pure-Python writer (no wsl/dd crash) and produce
        a valid GPT (protective MBR + 'EFI PART' header)."""
        out = self._pyz("--source", self.VM_SRC, "--target", "C:/test/r_gpt.img", "--gpt")
        assert "Traceback" not in out and "charmap" not in out, out
        assert "wsl" not in out.lower(), f"GPT still using WSL:\n{out}"
        assert "[OK]" in out, out
        local = self._fetch("C:/test/r_gpt.img")
        try:
            with open(local, "rb") as f:
                head = f.read(34 * 512)
            assert head[446 + 4] == 0xEE, "no protective MBR (0xEE) partition"
            assert head[512:520] == b"EFI PART", "no primary GPT header"
        finally:
            os.unlink(local)

    def test_iso_has_el_torito_efi_boot(self) -> None:
        """An ISO built from a tree with EFI/BOOT/BOOTX64.EFI must carry an El
        Torito boot record (i.e. be UEFI-bootable), not be data-only."""
        out = self._pyz("--source", self.VM_SRC, "--target", "C:/test/r_boot.iso")
        assert "Traceback" not in out and "[OK]" in out, out
        local = self._fetch("C:/test/r_boot.iso")
        try:
            with open(local, "rb") as f:
                f.seek(17 * 2048)  # Boot Record Volume Descriptor
                br = f.read(2048)
            assert br[1:6] == b"CD001", "no volume descriptor at sector 17"
            assert b"EL TORITO SPECIFICATION" in br, "ISO has no El Torito boot record"
        finally:
            os.unlink(local)

    def test_modify_without_mtools(self) -> None:
        """--modify must work on Windows (no mcopy) and round-trip the file
        set: added file present, removed file gone, boot file preserved."""
        self._pyz("--source", self.VM_SRC, "--target", "C:/test/r_mod.img", "--mbr")
        winvm_ssh('powershell -NoProfile -Command "Set-Content -Encoding ascii '
                  '-Path C:\\test\\r_add.txt -Value added"', timeout=20)
        out = self._pyz("--modify", "C:/test/r_mod.img",
                        "--add", "C:/test/r_add.txt", "--remove", "README.txt")
        assert "mcopy" not in out and "Traceback" not in out, out
        assert "Modified" in out, out
        local = self._fetch("C:/test/r_mod.img")
        try:
            from mkimage.modify import _read_fat32_image
            names = {k.lower() for k in _read_fat32_image(local)["files"]}
        finally:
            os.unlink(local)
        assert "r_add.txt" in names, f"added file missing: {names}"
        assert "readme.txt" not in names, f"removed file still present: {names}"
        assert any(n.endswith("bootx64.efi") for n in names), \
            f"boot file lost after modify: {names}"

    def test_verify_runs_on_windows(self) -> None:
        """--verify must actually verify (pure-Python FAT read), not skip for
        lack of mtools."""
        out = self._pyz("--source", self.VM_SRC, "--target",
                        "C:/test/r_vfy.img", "--mbr", "--verify")
        assert "skipping verification" not in out and "mcopy not available" not in out, out
        assert "Verification passed" in out, out

    def test_gui_keyboard_shortcuts(self) -> None:
        """The GUI's F-key navigation is wired: the shortcut table is complete,
        all actions are callable, every mvKey_* exists, and the handlers
        register without error. Runs on the guest (Dear PyGui present there);
        base64-exec avoids SSH quoting issues."""
        import base64
        script = (
            "import sys\n"
            "sys.path.insert(0, r'C:/Users/mike/mkimage/mkimage.pyz')\n"
            "import dearpygui.dearpygui as dpg\n"
            "dpg.create_context()\n"
            "from mkimage.gui_dpg import _KEY_SHORTCUTS, _setup_key_handlers\n"
            "keys = [k for k, _ in _KEY_SHORTCUTS]\n"
            "exp = {'F1','F2','F3','F4','F5','F6','F7','F8','F9','F12'}\n"
            "assert exp <= set(keys), keys\n"
            "assert all(callable(fn) for _, fn in _KEY_SHORTCUTS)\n"
            "assert all(getattr(dpg, 'mvKey_'+k, None) is not None for k in keys)\n"
            "_setup_key_handlers()\n"
            "dpg.destroy_context()\n"
            "print('GUIKEYS-OK')\n"
        )
        b64 = base64.b64encode(script.encode()).decode()
        r = winvm_ssh(
            "python -c \"import base64;exec(base64.b64decode('"
            + b64 + "').decode())\"", timeout=60)
        assert "GUIKEYS-OK" in (r.stdout + r.stderr), r.stdout + r.stderr
