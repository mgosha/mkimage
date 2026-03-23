"""Tool detection, auto-install, and package resolution."""
from __future__ import annotations

from typing import TYPE_CHECKING

from mkimage.platform import _is_macos, _is_windows, _run, _which

if TYPE_CHECKING:
    from mkimage import Config

# Map tool names to packages per distro family
_TOOL_PACKAGES: dict[str, dict[str, str]] = {
    "mkfs.vfat": {"apt": "dosfstools",  "dnf": "dosfstools",  "pacman": "dosfstools",  "brew": "dosfstools"},
    "mcopy":     {"apt": "mtools",      "dnf": "mtools",      "pacman": "mtools",      "brew": "mtools"},
    "mmd":       {"apt": "mtools",      "dnf": "mtools",      "pacman": "mtools",      "brew": "mtools"},
    "rsync":     {"apt": "rsync",       "dnf": "rsync",       "pacman": "rsync",       "brew": "rsync"},
    "xorriso":   {"apt": "xorriso",     "dnf": "xorriso",     "pacman": "libisoburn",  "brew": "xorriso"},
    "genisoimage": {"apt": "genisoimage", "dnf": "genisoimage", "pacman": "cdrtools",  "brew": "cdrtools"},
    "sgdisk":      {"apt": "gdisk",       "dnf": "gdisk",       "pacman": "gptfdisk",  "brew": "gptfdisk"},
}


def _detect_pkg_manager() -> tuple[str, str]:
    """Detect the package manager. Returns (command, name)."""
    for cmd, name in [("apt-get", "apt"), ("dnf", "dnf"), ("yum", "dnf"),
                      ("pacman", "pacman"), ("brew", "brew")]:
        if _which(cmd):
            return cmd, name
    return "", ""


def _install_packages(cfg: Config, packages: list[str]) -> bool:
    """Attempt to install packages via the system package manager."""
    pkg_cmd, pkg_name = _detect_pkg_manager()
    if not pkg_cmd:
        return False

    # Deduplicate
    pkgs = sorted(set(packages))
    cfg.log(f"  Installing: {' '.join(pkgs)} (via {pkg_cmd})")

    if pkg_name == "pacman":
        cmd = [pkg_cmd, "-S", "--noconfirm"] + pkgs
    elif pkg_name == "brew":
        cmd = [pkg_cmd, "install"] + pkgs
    else:
        cmd = [pkg_cmd, "install", "-y"] + pkgs

    try:
        # brew runs as user, not root
        r = _run(cfg, cmd, check=False, verbose=True,
                 as_root=(pkg_name != "brew"))
        if r.returncode != 0:
            cfg.log(f"  Install failed: {r.stderr.strip()}")
            return False
        return True
    except Exception as e:
        cfg.log(f"  Install failed: {e}")
        return False


def _resolve_packages(tools: list[str]) -> list[str]:
    """Map missing tool names to installable package names."""
    _, pkg_name = _detect_pkg_manager()
    if not pkg_name:
        return tools
    pkgs = []
    for tool in tools:
        if tool in _TOOL_PACKAGES and pkg_name in _TOOL_PACKAGES[tool]:
            pkgs.append(_TOOL_PACKAGES[tool][pkg_name])
        else:
            pkgs.append(tool)
    return sorted(set(pkgs))


def check_tools_img() -> list[str]:
    """Check tools needed for FAT32 .img creation. Returns missing tools."""
    missing: list[str] = []
    for tool in ["dd", "mkfs.vfat", "mcopy"]:
        if not _which(tool):
            missing.append(tool)
    return missing


def check_tools_iso() -> list[str]:
    """Check tools needed for ISO creation. Returns missing tools."""
    if _which("xorriso"):
        return []
    if _which("genisoimage"):
        return []
    return ["xorriso"]


def check_tools_gpt() -> list[str]:
    """Check tools needed for GPT image creation. Returns missing tools."""
    missing: list[str] = []
    base_tools = ["dd", "mkfs.vfat", "rsync", "sgdisk"]
    # macOS uses hdiutil instead of losetup for loop devices
    if _is_macos():
        base_tools.append("hdiutil")
    else:
        base_tools.append("losetup")
    for tool in base_tools:
        if not _which(tool):
            missing.append(tool)
    return missing


def check_tools_mbr() -> list[str]:
    """Check tools needed for MBR image creation. Returns missing tools."""
    missing: list[str] = []
    # sfdisk on Linux, fdisk on macOS
    if _is_macos():
        base_tools = ["dd", "mkfs.vfat", "rsync", "fdisk"]
    else:
        base_tools = ["dd", "mkfs.vfat", "rsync", "sfdisk"]
    if _is_macos():
        base_tools.append("hdiutil")
    else:
        base_tools.append("losetup")
    for tool in base_tools:
        if not _which(tool):
            missing.append(tool)
    return missing


def check_tools_fs(fs_type: str) -> list[str]:
    """Check tools needed for a specific filesystem. Returns missing tools."""
    tools: dict[str, str] = {
        "fat32": "mkfs.vfat",
        "exfat": "mkfs.exfat",
        "ntfs": "mkfs.ntfs",
    }
    tool = tools.get(fs_type)
    if tool and not _which(tool):
        return [tool]
    return []


def ensure_tools(cfg: Config, fmt: str) -> None:
    """Check for required tools and auto-install if missing.

    Raises RuntimeError if tools cannot be installed.
    """
    if fmt == "img":
        missing = check_tools_img()
    elif fmt == "mbr":
        missing = check_tools_mbr()
    elif fmt == "gpt":
        missing = check_tools_gpt()
    else:
        missing = check_tools_iso()

    if not missing:
        return

    cfg.log(f"  Missing tools: {', '.join(missing)}")
    packages = _resolve_packages(missing)

    cfg.log(f"  Attempting auto-install...")
    if _install_packages(cfg, packages):
        # Verify installation worked
        if fmt == "img":
            still_missing = check_tools_img()
        elif fmt == "mbr":
            still_missing = check_tools_mbr()
        elif fmt == "gpt":
            still_missing = check_tools_gpt()
        else:
            still_missing = check_tools_iso()
        if not still_missing:
            cfg.log(f"  Tools installed successfully.")
            return

    # Failed -- give manual instructions
    pkg_cmd, _ = _detect_pkg_manager()
    if _is_windows():
        msg = f"Missing tools. Run in WSL:\n    sudo apt install {' '.join(packages)}"
    elif pkg_cmd:
        msg = f"Auto-install failed. Run manually:\n    sudo {pkg_cmd} install {' '.join(packages)}"
    else:
        msg = f"Missing tools: {', '.join(missing)}\n    Install packages: {' '.join(packages)}"
    raise RuntimeError(msg)


def _suggest_install(tool: str) -> str:
    """Return a platform-appropriate install command for a tool."""
    packages = _resolve_packages([tool])
    pkg_cmd, _ = _detect_pkg_manager()
    if pkg_cmd:
        return f"sudo {pkg_cmd} install {' '.join(packages)}"
    return f"Install package: {' '.join(packages)}"
