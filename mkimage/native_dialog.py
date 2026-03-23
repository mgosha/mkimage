"""Native OS file dialogs via subprocess.

Spawns a Python subprocess that opens tkinter.filedialog, prints the
selected path to stdout, and exits. This avoids event loop conflicts
when used alongside Dear PyGui or other GUI frameworks.

Fallback chain: tkinter.filedialog -> zenity/kdialog (Linux) -> empty list.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys


def native_file_dialog(
    mode: str,
    title: str = "",
    filetypes: list[tuple[str, str]] | None = None,
    initial_dir: str = "",
    multiple: bool = False,
) -> list[str]:
    """Open a native OS file dialog. Returns list of selected paths (empty if cancelled).

    Args:
        mode: "open_file", "open_dir", or "save_file"
        title: Dialog window title
        filetypes: List of (description, pattern) tuples for file filters
        initial_dir: Starting directory
        multiple: Allow multiple file selection (open_file only)
    """
    result = _try_tkinter(mode, title, filetypes, initial_dir, multiple)
    if result is not None:
        return result

    if platform.system() == "Linux":
        result = _try_zenity(mode, title, filetypes, initial_dir, multiple)
        if result is not None:
            return result
        result = _try_kdialog(mode, title, filetypes, initial_dir, multiple)
        if result is not None:
            return result

    return []


def _try_tkinter(
    mode: str, title: str, filetypes: list[tuple[str, str]] | None,
    initial_dir: str, multiple: bool,
) -> list[str] | None:
    """Open dialog via tkinter in a subprocess."""
    ft_arg = repr(filetypes) if filetypes else "None"
    script = (
        "import tkinter as tk; from tkinter import filedialog; "
        "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True); "
    )
    if mode == "open_dir":
        script += (
            f"r = filedialog.askdirectory(title={title!r}, "
            f"initialdir={initial_dir!r}); "
            "print(r if r else '')"
        )
    elif mode == "open_file" and multiple:
        script += (
            f"r = filedialog.askopenfilenames(title={title!r}, "
            f"initialdir={initial_dir!r}, "
            f"filetypes={ft_arg}); "
            "print('\\n'.join(r) if r else '')"
        )
    elif mode == "open_file":
        script += (
            f"r = filedialog.askopenfilename(title={title!r}, "
            f"initialdir={initial_dir!r}, "
            f"filetypes={ft_arg}); "
            "print(r if r else '')"
        )
    elif mode == "save_file":
        script += (
            f"r = filedialog.asksaveasfilename(title={title!r}, "
            f"initialdir={initial_dir!r}, "
            f"filetypes={ft_arg}); "
            "print(r if r else '')"
        )
    else:
        return None

    try:
        r = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            return None
        paths = [p for p in r.stdout.strip().splitlines() if p]
        return paths
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _try_zenity(
    mode: str, title: str, filetypes: list[tuple[str, str]] | None,
    initial_dir: str, multiple: bool,
) -> list[str] | None:
    """Open dialog via zenity (GNOME/GTK)."""
    cmd = ["zenity", "--file-selection", f"--title={title or 'Select'}"]
    if mode == "open_dir":
        cmd.append("--directory")
    elif mode == "save_file":
        cmd.append("--save")
    if multiple and mode == "open_file":
        cmd.append("--multiple")
        cmd.append("--separator=\n")
    if initial_dir:
        cmd.append(f"--filename={initial_dir}{os.sep}")
    if filetypes:
        for desc, pattern in filetypes:
            if pattern != "*.*" and pattern != "*":
                cmd.append(f"--file-filter={desc} | {pattern}")

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return [] if r.returncode == 1 else None  # 1 = cancel
        paths = [p for p in r.stdout.strip().splitlines() if p]
        return paths
    except FileNotFoundError:
        return None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _try_kdialog(
    mode: str, title: str, filetypes: list[tuple[str, str]] | None,
    initial_dir: str, multiple: bool,
) -> list[str] | None:
    """Open dialog via kdialog (KDE)."""
    start = initial_dir or os.path.expanduser("~")
    if mode == "open_dir":
        cmd = ["kdialog", "--getexistingdirectory", start, "--title", title or "Select"]
    elif mode == "save_file":
        cmd = ["kdialog", "--getsavefilename", start, "--title", title or "Select"]
    elif mode == "open_file":
        cmd = ["kdialog", "--getopenfilename", start, "--title", title or "Select"]
        if multiple:
            cmd[1] = "--getopenfilename"  # kdialog doesn't have --multiple
    else:
        return None

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return [] if r.returncode == 1 else None
        paths = [p for p in r.stdout.strip().splitlines() if p]
        return paths
    except FileNotFoundError:
        return None
    except (subprocess.TimeoutExpired, OSError):
        return None
