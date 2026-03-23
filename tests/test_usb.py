"""USB safety logic tests — all mocked, no real devices touched."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from mkimage import (
    Config,
    MAX_USB_SIZE_GB,
    _list_removable_drives_linux,
    write_usb,
)

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
        with patch("mkimage._run", side_effect=_make_run_mock().side_effect):
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
        with patch("mkimage._run", side_effect=_make_run_mock(lsblk).side_effect):
            drives = _list_removable_drives_linux()
        assert len(drives) == 0

    def test_filter_large_drives(self) -> None:
        # 500GB USB drive should be filtered (> 256GB limit)
        big = f"sdb {500 * 1024**3} 1 disk Big USB usb\n"
        with patch("mkimage._run", side_effect=_make_run_mock(big).side_effect):
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

        with patch("mkimage._run", side_effect=fake_run):
            drives = _list_removable_drives_linux()
        assert len(drives) == 0


class TestWriteUsbSafety:
    def test_abort_no_drives(self) -> None:
        messages: list[str] = []
        cfg = Config(log=lambda msg: messages.append(msg))
        with patch("mkimage._list_removable_drives", return_value=[]):
            write_usb(cfg, "fake.img")
        assert any("no removable" in m.lower() for m in messages)

    def test_abort_no_confirm(self) -> None:
        messages: list[str] = []
        cfg = Config(log=lambda msg: messages.append(msg))
        fake_drive = {
            "name": "sdb", "path": "/dev/sdb", "size": "16GB",
            "size_bytes": str(16 * 1024**3), "model": "Test USB",
        }
        with patch("mkimage._list_removable_drives", return_value=[fake_drive]):
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
            "name": "sdb", "path": "/dev/sdb", "size": "500GB",
            "size_bytes": str(500 * 1024**3), "model": "Huge Drive",
        }
        with patch("mkimage._list_removable_drives", return_value=[huge_drive]):
            write_usb(
                cfg, "fake.img",
                select_drive=lambda drives: huge_drive,
                confirm_write=lambda target: True,
            )
        assert any("256gb" in m.lower() or "refusing" in m.lower() for m in messages)

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

        with patch("mkimage._list_removable_drives", return_value=[sda_drive]), \
             patch("mkimage._run", side_effect=fake_run):
            write_usb(
                cfg, "fake.img",
                select_drive=lambda drives: sda_drive,
                confirm_write=lambda target: True,
            )
        assert any("system" in m.lower() or "refusing" in m.lower() for m in messages)
