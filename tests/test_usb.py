"""USB safety logic tests — all mocked, no real devices touched."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mkimage import Config, _detect_source_type, _detect_target_type, _usb_safety_checks, _verify_usb_bus, write_usb
from mkimage.usb.detect import MAX_USB_SIZE_GB, _list_removable_drives_linux

# Sample lsblk output for mocking
LSBLK_OUTPUT = """\
sda  500107862016 0 disk Samsung SSD 970  nvme
sdb  15728640000 1 disk  USB Flash Drive usb
sdc  32212254720 1 disk  Kingston DT 100 usb
nvme0n1 512110190592 0 disk Samsung SSD 980 nvme
"""

LSBLK_MOUNTPOINTS_NONE = ""
LSBLK_MOUNTPOINTS_ROOT = "/"


def _make_run_mock(lsblk_output: str = LSBLK_OUTPUT,
                   mountpoints: str = LSBLK_MOUNTPOINTS_NONE) -> MagicMock:
    """Create a mock for _run that returns fake lsblk output."""
    def fake_run(cfg: object, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "lsblk" and "--bytes" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=lsblk_output, stderr="")
        if cmd[0] == "lsblk" and "MOUNTPOINT" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=mountpoints, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return MagicMock(side_effect=fake_run)


class TestListDrivesLinux:
    def test_parse_lsblk(self) -> None:
        with patch("mkimage.usb.detect._run", side_effect=_make_run_mock().side_effect):
            drives = _list_removable_drives_linux()
        names = [d["name"] for d in drives]
        # sdb and sdc are removable USB, sda is not removable, nvme is not
        assert "sdb" in names
        assert "sdc" in names
        assert "sda" not in names
        assert "nvme0n1" not in names

    def test_filter_non_removable(self) -> None:
        # sda has removable=0 and non-USB transport
        lsblk = "sda 500107862016 0 disk Samsung SSD sata\n"
        with patch("mkimage.usb.detect._run", side_effect=_make_run_mock(lsblk).side_effect):
            drives = _list_removable_drives_linux()
        assert len(drives) == 0

    def test_filter_large_drives(self) -> None:
        # 4TB USB drive should be filtered (> 2TB limit)
        big = f"sdb {4 * 1024**4} 1 disk Big USB usb\n"
        with patch("mkimage.usb.detect._run", side_effect=_make_run_mock(big).side_effect):
            drives = _list_removable_drives_linux()
        assert len(drives) == 0

    def test_filter_root_mount(self) -> None:
        lsblk = "sdb 15728640000 1 disk USB Flash usb\n"

        def fake_run(cfg: object, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if cmd[0] == "lsblk" and "--bytes" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=lsblk, stderr="")
            if cmd[0] == "lsblk" and "MOUNTPOINT" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="/\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("mkimage.usb.detect._run", side_effect=fake_run):
            drives = _list_removable_drives_linux()
        assert len(drives) == 0


class TestWriteUsbSafety:
    def test_abort_no_drives(self) -> None:
        messages: list[str] = []
        cfg = Config(log=lambda msg: messages.append(msg))
        with patch("mkimage.usb.write._list_removable_drives", return_value=[]):
            write_usb(cfg, "fake.img")
        assert any("no removable" in m.lower() for m in messages)

    def test_abort_no_confirm(self) -> None:
        messages: list[str] = []
        cfg = Config(log=lambda msg: messages.append(msg))
        fake_drive = {
            "name": "sdb", "path": "/dev/sdb", "size": "16GB",
            "size_bytes": str(16 * 1024**3), "model": "Test USB",
        }
        with patch("mkimage.usb.write._list_removable_drives", return_value=[fake_drive]):
            write_usb(
                cfg, "fake.img",
                select_drive=lambda drives: fake_drive,
                confirm_write=lambda target: False,
            )
        assert any("aborted" in m.lower() for m in messages)

    def test_size_limit_enforced(self) -> None:
        messages: list[str] = []
        cfg = Config(log=lambda msg: messages.append(msg))
        huge_drive = {
            "name": "sdb", "path": "/dev/sdb", "size": "4TB",
            "size_bytes": str(4 * 1024**4), "model": "Huge Drive",
        }
        with patch("mkimage.usb.write._list_removable_drives", return_value=[huge_drive]):
            write_usb(
                cfg, "fake.img",
                select_drive=lambda drives: huge_drive,
                confirm_write=lambda target: True,
            )
        assert any("2048gb" in m.lower() or "refusing" in m.lower() for m in messages)

    def test_reject_sda_system(self) -> None:
        messages: list[str] = []
        cfg = Config(log=lambda msg: messages.append(msg))
        sda_drive = {
            "name": "sda", "path": "/dev/sda", "size": "120GB",
            "size_bytes": str(120 * 1024**3), "model": "System Disk",
        }

        def fake_run(cfg_arg: object, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if "MOUNTPOINT" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="/\n/boot\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("mkimage.usb.write._list_removable_drives", return_value=[sda_drive]), \
             patch("mkimage.usb.write._run", side_effect=fake_run):
            write_usb(
                cfg, "fake.img",
                select_drive=lambda drives: sda_drive,
                confirm_write=lambda target: True,
            )
        assert any("system" in m.lower() or "refusing" in m.lower() for m in messages)


class TestVerifyUsbBus:
    def test_accepts_usb(self) -> None:
        cfg = Config()
        udevadm_output = "ID_BUS=usb\nID_USB_DRIVER=usb-storage\n"

        def fake_run(cfg_arg: object, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if cmd[0] == "which":
                return subprocess.CompletedProcess(cmd, 0, stdout="/usr/bin/udevadm", stderr="")
            if cmd[0] == "udevadm":
                return subprocess.CompletedProcess(cmd, 0, stdout=udevadm_output, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("mkimage.usb.safety._run", side_effect=fake_run):
            assert _verify_usb_bus(cfg, "/dev/sdb") is True

    def test_rejects_sata(self) -> None:
        cfg = Config()
        udevadm_output = "ID_BUS=ata\nID_TYPE=disk\n"

        def fake_run(cfg_arg: object, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if cmd[0] == "which":
                return subprocess.CompletedProcess(cmd, 0, stdout="/usr/bin/udevadm", stderr="")
            if cmd[0] == "udevadm":
                return subprocess.CompletedProcess(cmd, 0, stdout=udevadm_output, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("mkimage.usb.safety._run", side_effect=fake_run):
            assert _verify_usb_bus(cfg, "/dev/sda") is False

    def test_skips_if_unavailable(self) -> None:
        cfg = Config()

        def fake_which(tool: str) -> bool:
            return tool != "udevadm"

        with patch("mkimage.usb.safety._which", side_effect=fake_which):
            assert _verify_usb_bus(cfg, "/dev/sdb") is True


class TestUsbSafetyChecks:
    def test_rejects_non_usb_bus(self) -> None:
        cfg = Config()
        drive = {"name": "sdb", "path": "/dev/sdb", "size": "16GB",
                 "size_bytes": str(16 * 1024**3), "model": "SSD"}

        with patch("mkimage.usb.safety._verify_usb_bus", return_value=False):
            assert _usb_safety_checks(cfg, drive) is False

    def test_rejects_oversized(self) -> None:
        cfg = Config()
        drive = {"name": "sdb", "path": "/dev/sdb", "size": "4TB",
                 "size_bytes": str(4 * 1024**4), "model": "Big Drive"}

        with patch("mkimage.usb.safety._verify_usb_bus", return_value=True):
            assert _usb_safety_checks(cfg, drive) is False

    def test_accepts_valid_usb(self) -> None:
        cfg = Config()
        drive = {"name": "sdb", "path": "/dev/sdb", "size": "16GB",
                 "size_bytes": str(16 * 1024**3), "model": "USB Flash"}

        with patch("mkimage.usb.safety._verify_usb_bus", return_value=True):
            assert _usb_safety_checks(cfg, drive) is True


class TestDetectSourceType:
    def test_directory(self, tmp_path: Path) -> None:
        assert _detect_source_type(str(tmp_path)) == "directory"

    def test_image_file(self, tmp_path: Path) -> None:
        img = tmp_path / "test.img"
        img.write_bytes(b"\x00")
        assert _detect_source_type(str(img)) == "image"

    def test_iso_file(self, tmp_path: Path) -> None:
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00")
        assert _detect_source_type(str(iso)) == "image"

    def test_nonexistent_raises(self) -> None:
        with pytest.raises(ValueError):
            _detect_source_type("/nonexistent/path")


class TestDetectTargetType:
    def test_img(self) -> None:
        assert _detect_target_type("output.img") == "img"

    def test_iso(self) -> None:
        assert _detect_target_type("output.iso") == "iso"

    def test_device(self) -> None:
        assert _detect_target_type("/dev/sdb") == "device"

    def test_usb_auto(self) -> None:
        assert _detect_target_type("usb") == "usb-auto"
        assert _detect_target_type("USB") == "usb-auto"

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError):
            _detect_target_type("output.txt")

