"""Platform detection, command execution, and path utilities."""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mkimage import Config


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _wsl_path(win_path: str) -> str:
    """Convert a Windows path to a WSL /mnt/c/... path."""
    p = Path(win_path).resolve()
    drive = p.drive.rstrip(":").lower()
    rest = str(p).replace(p.drive, "").replace("\\", "/")
    return f"/mnt/{drive}{rest}"


def _run(cfg: Config, cmd: list[str], check: bool = True,
         verbose: bool = False,
         as_root: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a command, routing through WSL on Windows.

    Args:
        cfg: Runtime configuration (used for logging).
        verbose: Log this specific command's invocation and output.
        as_root: Run as root. On Windows uses 'wsl -u root'. On Linux uses 'sudo'.
    """
    # On macOS, resolve tool paths (Homebrew sbin may not be in PATH)
    if _is_macos() and cmd and not cmd[0].startswith("/"):
        cmd = [_find_tool(cmd[0])] + cmd[1:]

    if _is_windows():
        shell_cmd = " ".join(_shell_quote(c) for c in cmd)
        if as_root:
            actual = ["wsl", "-u", "root", "bash", "-c", shell_cmd]
        else:
            actual = ["wsl", "bash", "-c", shell_cmd]
    else:
        if as_root:
            actual = ["sudo"] + cmd
        else:
            actual = cmd
    if verbose:
        prefix = "(root) " if as_root else ""
        cfg.log(f"  > {prefix}{' '.join(cmd)}")
    result = subprocess.run(actual, check=check, capture_output=True, text=True)
    if verbose and result.stdout.strip():
        for line in result.stdout.strip().splitlines():
            cfg.log(f"  {line}")
    if verbose and result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            cfg.log(f"  {line}")
    return result


def _shell_quote(s: str) -> str:
    """Shell-quote a string for bash."""
    if all(c.isalnum() or c in "-_./=:" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def _which(tool: str) -> bool:
    """Check if a tool is available (via WSL on Windows)."""
    from mkimage import Config
    _quiet = Config()  # silent config for tool probing
    try:
        r = _run(_quiet, ["which", tool], check=False)
        if r.returncode == 0:
            return True
    except FileNotFoundError:
        pass
    # On macOS, Homebrew sbin may not be in PATH
    if _is_macos():
        for sbin in ["/usr/local/sbin", "/opt/homebrew/sbin"]:
            if Path(f"{sbin}/{tool}").exists():
                return True
    return False


def _find_tool(tool: str) -> str:
    """Find a tool's full path, checking Homebrew sbin on macOS."""
    if _is_macos():
        for sbin in ["/usr/local/sbin", "/opt/homebrew/sbin",
                     "/usr/local/bin", "/opt/homebrew/bin"]:
            full = f"{sbin}/{tool}"
            if Path(full).exists():
                return full
    return tool  # return as-is, let PATH handle it


_ps1_cache: str = ""


def _find_ps1() -> str:
    """Find mkimage.ps1 relative to this package.

    Checks filesystem paths first, then extracts from the package
    (zipapp) if needed. Extracted file is cached for the session.
    """
    global _ps1_cache
    if _ps1_cache:
        return _ps1_cache

    # Check filesystem paths (normal install)
    candidates = [
        Path(__file__).parent.parent / "mkimage.ps1",  # next to mkimage/ package
        Path(__file__).parent / "mkimage.ps1",          # inside package dir
    ]
    for c in candidates:
        if c.exists():
            _ps1_cache = str(c)
            return _ps1_cache

    # Try extracting from package data (zipapp)
    try:
        import importlib.resources as pkg_resources
        try:
            # Python 3.9+
            ref = pkg_resources.files("mkimage").joinpath("mkimage.ps1")
            ps1_path = str(ref)
            if Path(ps1_path).exists():
                _ps1_cache = ps1_path
                return _ps1_cache
        except (AttributeError, TypeError):
            pass

        # Fallback: read and write to temp file
        try:
            data = pkg_resources.read_text("mkimage", "mkimage.ps1")
        except (FileNotFoundError, ModuleNotFoundError):
            data = None

        if data:
            import tempfile
            fd, tmp_path = tempfile.mkstemp(suffix=".ps1", prefix="mkimage_")
            import os
            os.write(fd, data.encode("utf-8"))
            os.close(fd)
            _ps1_cache = tmp_path
            return _ps1_cache
    except Exception:
        pass

    return ""


def _resolve(path: str) -> str:
    """Resolve a path for the execution environment (WSL or native)."""
    if _is_windows():
        return _wsl_path(path)
    return str(Path(path).resolve())
