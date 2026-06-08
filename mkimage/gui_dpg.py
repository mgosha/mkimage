"""mkimage GUI -- Dear PyGui interface with tabbed layout.

Modern GPU-accelerated GUI. Falls back to Tkinter if dearpygui is not
installed. Requires: pip install dearpygui
"""
from __future__ import annotations

import math
import os
import threading
from pathlib import Path, PurePosixPath

import dearpygui.dearpygui as dpg

from mkimage import (
    Config,
    PartitionSpec,
    _is_compressed_path,
    _is_windows,
    _list_removable_drives,
    _strip_compression_ext,
    _write_usb_from_dir,
    _write_usb_from_image,
    _compress_file,
    build_gpt_img,
    build_img,
    build_iso,
    collect_files,
    get_available_filesystems,
)

# ---------------------------------------------------------------------------
# Thread-safe logging
# ---------------------------------------------------------------------------

_log_lines: list[str] = []
_log_lock = threading.Lock()
_building = False


def _log(msg: str) -> None:
    with _log_lock:
        _log_lines.append(msg + "\n")


def _flush_log() -> None:
    with _log_lock:
        if not _log_lines:
            return
        batch = "".join(_log_lines)
        _log_lines.clear()
    current = dpg.get_value("log_text") or ""
    dpg.set_value("log_text", current + batch)
    dpg.set_y_scroll("log_child", dpg.get_y_scroll_max("log_child"))


def _set_status(text: str) -> None:
    dpg.set_value("status_text", text)


# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------

_ACCENT = (65, 130, 215)
_ACCENT_HOVER = (90, 155, 235)
_ACCENT_ACTIVE = (45, 100, 180)
_BG_DARK = (25, 25, 30)
_BG_FRAME = (38, 38, 45)
_BG_CHILD = (30, 30, 36)
_TEXT = (220, 220, 225)
_TEXT_DIM = (140, 140, 150)
_HEADER = (100, 180, 255)
_SUCCESS = (80, 200, 120)
_ERROR = (220, 80, 80)
_BORDER = (55, 55, 65)
_GREEN_BTN = (40, 150, 80)
_GREEN_HOVER = (55, 175, 100)
_GREEN_ACTIVE = (30, 120, 65)


def _create_themes() -> None:
    with dpg.theme(tag="main_theme"):
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 5)
            dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, 5)
            dpg.add_theme_style(dpg.mvStyleVar_TabRounding, 5)
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 12, 10)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 8, 4)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 5)
            dpg.add_theme_style(dpg.mvStyleVar_ScrollbarSize, 12)
            dpg.add_theme_style(dpg.mvStyleVar_ScrollbarRounding, 5)
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, _BG_DARK)
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, _BG_CHILD)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, _BG_FRAME)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (50, 50, 60))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (55, 55, 68))
            dpg.add_theme_color(dpg.mvThemeCol_Text, _TEXT)
            dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, _TEXT_DIM)
            dpg.add_theme_color(dpg.mvThemeCol_Border, _BORDER)
            dpg.add_theme_color(dpg.mvThemeCol_Button, _ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, _ACCENT_HOVER)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, _ACCENT_ACTIVE)
            dpg.add_theme_color(dpg.mvThemeCol_CheckMark, _ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_Tab, (40, 40, 50))
            dpg.add_theme_color(dpg.mvThemeCol_TabHovered, (60, 60, 75))
            dpg.add_theme_color(dpg.mvThemeCol_TabActive, (50, 55, 70))
            dpg.add_theme_color(dpg.mvThemeCol_Separator, _BORDER)
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, (20, 20, 25))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, (60, 60, 75))
            dpg.add_theme_color(dpg.mvThemeCol_PopupBg, (35, 35, 42))

    with dpg.theme(tag="action_theme"):
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, _GREEN_BTN)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, _GREEN_HOVER)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, _GREEN_ACTIVE)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 6)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 12, 8)

    with dpg.theme(tag="log_theme"):
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (18, 18, 22))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (18, 18, 22))
            dpg.add_theme_color(dpg.mvThemeCol_Text, (180, 200, 180))
            dpg.add_theme_color(dpg.mvThemeCol_Border, (40, 40, 48))

    with dpg.theme(tag="header_theme"):
        with dpg.theme_component(dpg.mvText):
            dpg.add_theme_color(dpg.mvThemeCol_Text, _HEADER)

    with dpg.theme(tag="status_theme"):
        with dpg.theme_component(dpg.mvText):
            dpg.add_theme_color(dpg.mvThemeCol_Text, _TEXT_DIM)

    for name, color in [("progress_theme", _ACCENT),
                        ("progress_success", _SUCCESS),
                        ("progress_error", _ERROR)]:
        with dpg.theme(tag=name):
            with dpg.theme_component(dpg.mvProgressBar):
                dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram, color)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (35, 35, 42))


# ---------------------------------------------------------------------------
# Native file dialog helper
# ---------------------------------------------------------------------------

_dialog_open = False


def _open_native_dialog(
    mode: str, title: str, callback: object,
    filetypes: list[tuple[str, str]] | None = None,
    multiple: bool = False,
) -> None:
    """Open a native OS file dialog in a background thread. Only one at a time."""
    global _dialog_open
    if _dialog_open:
        return
    _dialog_open = True

    def run() -> None:
        global _dialog_open
        try:
            from mkimage.native_dialog import native_file_dialog
            results = native_file_dialog(
                mode=mode, title=title, filetypes=filetypes, multiple=multiple)
            if results:
                callback(results)
        finally:
            _dialog_open = False
    threading.Thread(target=run, daemon=True).start()


def _browse_source() -> None:
    _open_native_dialog("open_dir", "Select Source Directory",
                        lambda r: dpg.set_value("source_path", r[0]))

def _browse_include_file() -> None:
    _open_native_dialog(
        "open_file", "Select File to Include",
        lambda r: [dpg.add_selectable(label=p, parent="includes_list") for p in r],
        filetypes=[("EFI Applications", "*.efi"), ("UEFI Shell Scripts", "*.nsh"),
                   ("All Files", "*.*")],
        multiple=True)

def _browse_include_dir() -> None:
    _open_native_dialog("open_dir", "Select Directory to Include",
                        lambda r: dpg.add_selectable(label=r[0], parent="includes_list"))

def _browse_output() -> None:
    _open_native_dialog(
        "save_file", "Save Image As",
        lambda r: dpg.set_value("output_path", r[0]),
        filetypes=[("FAT32 Image", "*.img"), ("ISO Image", "*.iso"),
                   ("Compressed Image", "*.img.gz"), ("All Files", "*.*")])

# ---------------------------------------------------------------------------
# UI callbacks
# ---------------------------------------------------------------------------

def _refresh_drives() -> None:
    drives = _list_removable_drives()
    items = [f"{d['path']}  {d['size']}  {d['model']}" for d in drives] or ["(no USB drives found)"]
    dpg.configure_item("drive_combo", items=items)
    dpg.set_value("drive_combo", items[0])
    _set_status(f"Found {len(drives)} USB drive(s)" if drives else "No USB drives found")

# ---------------------------------------------------------------------------
# Partition list editor
# ---------------------------------------------------------------------------

_partition_rows: list[int] = []


def _add_partition_row(sender: object = None, app_data: object = None,
                       fs: str = "fat32", size: str = "",
                       label: str = "UEFITOOLS", src: str = "",
                       cluster: str = "") -> None:
    """Add a partition row to the list."""
    row_id = dpg.add_group(horizontal=True, parent="partition_list")
    all_types = ["esp", "fat32", "exfat", "ntfs", "ext4", "udf"]
    avail = get_available_filesystems()
    items = [t if t in avail else f"{t} (n/a)" for t in all_types]
    dpg.add_combo(items, default_value=fs, width=80, parent=row_id)
    dpg.add_input_text(default_value=size, width=55, hint="Size",
                       parent=row_id)
    dpg.add_input_text(default_value=label, width=75, hint="Label",
                       parent=row_id)
    dpg.add_input_text(default_value=cluster, width=55, hint="Clust.",
                       parent=row_id)
    dpg.add_input_text(default_value=src, width=-1, hint="Source dir",
                       parent=row_id)
    _partition_rows.append(row_id)


def _remove_partition_row(sender: object = None,
                          app_data: object = None) -> None:
    """Remove the last partition row."""
    if len(_partition_rows) > 1:
        row_id = _partition_rows.pop()
        dpg.delete_item(row_id)


def _clear_partition_rows() -> None:
    """Remove all partition rows."""
    for row_id in _partition_rows:
        dpg.delete_item(row_id)
    _partition_rows.clear()


def _on_partition_scheme_change(sender: object = None,
                                app_data: object = None) -> None:
    """Reset partition rows when scheme changes."""
    val = dpg.get_value("partition_radio")
    _clear_partition_rows()
    if val == "GPT":
        _add_partition_row(fs="esp", label="ESP")
    elif val == "MBR":
        _add_partition_row(fs="fat32", label="UEFITOOLS")
    else:
        _add_partition_row(fs="fat32", size="+32M", label="UEFITOOLS")


def _get_partitions() -> list[PartitionSpec]:
    """Read partition specs from GUI rows."""
    partitions: list[PartitionSpec] = []
    for row_id in _partition_rows:
        children = dpg.get_item_children(row_id, 1)
        if len(children) >= 5:
            cs_str = dpg.get_value(children[3]).strip()
            cs = int(cs_str) if cs_str.isdigit() else 0
            fs_raw = dpg.get_value(children[0]).split(" ")[0]  # strip " (n/a)"
            partitions.append(PartitionSpec(
                fs_type=fs_raw,
                size=dpg.get_value(children[1]),
                label=dpg.get_value(children[2]) or "UEFITOOLS",
                cluster_size=cs,
                source_dir=dpg.get_value(children[4]),
            ))
    return partitions


def _check_drive(sender: object = None, app_data: object = None) -> None:
    """Run bad block check on the selected USB drive."""
    from mkimage.usb.safety import _check_bad_blocks, _unmount_device
    sel = dpg.get_value("drive_combo")
    if not sel or "no USB" in sel:
        _log("No USB drive selected.")
        return
    device = sel.split()[0]  # extract /dev/sdX from combo text
    _log(f"Checking {device} for bad blocks (destructive)...")
    _set_building(True)
    dpg.set_value("log_text", "")

    def run() -> None:
        cfg = Config(log=_log, verbose=True)
        try:
            _unmount_device(cfg, device)
            if _check_bad_blocks(cfg, device):
                _log(f"[OK] No bad blocks found on {device}.")
                _set_building(False, "success")
            else:
                _log(f"[FAIL] Bad blocks detected on {device}!")
                _set_building(False, "error")
        except Exception as e:
            _log(f"Error: {e}")
            _set_building(False, "error")

    import threading
    threading.Thread(target=run, daemon=True).start()


def _wipe_drive(sender: object = None, app_data: object = None) -> None:
    """Wipe all partition signatures from the selected target USB drive."""
    from mkimage.usb.safety import _wipe_device, _unmount_device
    sel = dpg.get_value("drive_combo")
    if not sel or "no USB" in sel:
        _log("No USB drive selected.")
        return
    device = sel.split()[0]
    _log(f"Wiping all signatures from {device}...")
    _set_building(True)

    def run() -> None:
        cfg = Config(log=_log, verbose=True)
        try:
            _unmount_device(cfg, device)
            _wipe_device(cfg, device)
            _log(f"[OK] {device} wiped — all partition signatures removed.")
            _set_building(False, "success")
        except Exception as e:
            _log(f"Error: {e}")
            _set_building(False, "error")

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Tools tab callbacks
# ---------------------------------------------------------------------------

def _refresh_tools_drives() -> None:
    drives = _list_removable_drives()
    items = ([f"{d['path']}  {d['size']}  {d['model']}" for d in drives]
             or ["(no USB drives found)"])
    dpg.configure_item("tools_drive_combo", items=items)
    dpg.set_value("tools_drive_combo", items[0])


def _tools_get_device() -> str | None:
    sel = dpg.get_value("tools_drive_combo")
    if not sel or "no USB" in sel or "click Refresh" in sel:
        _log("No USB drive selected. Click Refresh first.")
        return None
    return sel.split()[0]


def _tools_format_drive() -> None:
    device = _tools_get_device()
    if not device:
        return
    scheme = dpg.get_value("tools_scheme_radio")
    fs = dpg.get_value("tools_fs_combo")
    label = dpg.get_value("tools_label").strip() or "UEFITOOLS"
    _set_building(True)
    dpg.set_value("log_text", "")

    def run() -> None:
        from mkimage.usb.write import format_device
        from mkimage.usb.safety import _unmount_device
        cfg = Config(
            verbose=True, log=_log,
            gpt=(scheme == "GPT"), mbr=(scheme == "MBR"),
            label=label,
            partitions=[PartitionSpec(fs_type=fs, size="0", label=label)],
        )
        try:
            _unmount_device(cfg, device)
            format_device(cfg, device)
            _log(f"[OK] {device} formatted as {fs} ({scheme}).")
            _set_building(False, "success")
        except Exception as e:
            _log(f"Error: {e}")
            _set_building(False, "error")

    threading.Thread(target=run, daemon=True).start()


def _tools_wipe_drive() -> None:
    device = _tools_get_device()
    if not device:
        return
    _log(f"Wiping all signatures from {device}...")
    _set_building(True)

    def run() -> None:
        from mkimage.usb.safety import _wipe_device, _unmount_device
        cfg = Config(log=_log, verbose=True)
        try:
            _unmount_device(cfg, device)
            _wipe_device(cfg, device)
            _log(f"[OK] {device} wiped.")
            _set_building(False, "success")
        except Exception as e:
            _log(f"Error: {e}")
            _set_building(False, "error")

    threading.Thread(target=run, daemon=True).start()


def _tools_check_drive() -> None:
    device = _tools_get_device()
    if not device:
        return
    _log(f"Checking {device} for bad blocks...")
    _set_building(True)
    dpg.set_value("log_text", "")

    def run() -> None:
        from mkimage.usb.safety import _check_bad_blocks, _unmount_device
        cfg = Config(log=_log, verbose=True)
        try:
            _unmount_device(cfg, device)
            if _check_bad_blocks(cfg, device):
                _log(f"[OK] No bad blocks on {device}.")
                _set_building(False, "success")
            else:
                _log(f"[FAIL] Bad blocks detected on {device}!")
                _set_building(False, "error")
        except Exception as e:
            _log(f"Error: {e}")
            _set_building(False, "error")

    threading.Thread(target=run, daemon=True).start()


def _browse_list_image() -> None:
    _open_native_dialog(
        "open_file", "Select Image File",
        lambda r: dpg.set_value("tools_image_path", r[0]),
        filetypes=[("Disk Image", "*.img"), ("All Files", "*.*")])


def _tools_list_image() -> None:
    path = dpg.get_value("tools_image_path").strip()
    if not path:
        _log("Error: No image file specified.")
        return
    dpg.set_value("log_text", "")
    dpg.set_value("tab_bar", dpg.get_alias_id("log_tab"))
    from mkimage.inspect import list_image
    list_image(path, _log)


def _on_source_mode_change(sender: int) -> None:
    mode = dpg.get_value(sender)
    dpg.hide_item("source_file_group")
    dpg.hide_item("source_includes_group")
    dpg.hide_item("source_usb_group")
    if mode == "USB drive (clone)":
        dpg.show_item("source_usb_group")
        _refresh_source_drives()
    else:
        dpg.show_item("source_file_group")
        dpg.show_item("source_includes_group")
    _update_action_label()


def _refresh_source_drives() -> None:
    drives = _list_removable_drives()
    items = [f"{d['path']}  {d['size']}  {d['model']}" for d in drives] or ["(no USB drives)"]
    dpg.configure_item("source_drive_combo", items=items)
    dpg.set_value("source_drive_combo", items[0])


def _on_target_mode_change(sender: int) -> None:
    mode = dpg.get_value(sender)
    if mode == "USB":
        dpg.hide_item("target_file_group")
        dpg.show_item("target_usb_group")
        _refresh_drives()
    else:
        dpg.show_item("target_file_group")
        dpg.hide_item("target_usb_group")
    _update_action_label()


def _on_fmt_change(sender: int = 0) -> None:
    """Extra (MB) sizes a raw .img; it's irrelevant for an ISO (which carries
    its own filesystem), so disable it when ISO is selected. (Format itself is
    already hidden for a USB target -- it lives in target_file_group.)"""
    is_iso = dpg.get_value("fmt_radio") == "ISO (.iso)"
    dpg.configure_item("extra_space", enabled=not is_iso)


def _update_action_label() -> None:
    src = dpg.get_value("source_mode")
    tgt = dpg.get_value("target_mode")
    if src == "Folder / image file" and tgt == "File":
        label = "Create Image"
    elif src == "Folder / image file" and tgt == "USB":
        label = "Write to USB"
    elif src == "USB drive (clone)" and tgt == "File":
        label = "Clone to Image"
    else:
        label = "Clone to USB"
    dpg.configure_item("action_btn", label=label)

def _get_includes() -> list[str]:
    includes: list[str] = []
    for child in (dpg.get_item_children("includes_list", 1) or []):
        label = dpg.get_item_label(child)
        if label:
            includes.append(label)
    return includes

def _set_building(active: bool, result: str = "") -> None:
    global _building
    _building = active
    dpg.configure_item("action_btn", enabled=not active)
    if active:
        dpg.bind_item_theme("progress_bar", "progress_theme")
        dpg.set_value("progress_bar", 0.0)
        dpg.show_item("progress_bar")
        _set_status("Building...")
        # Switch to Log tab
        dpg.set_value("tab_bar", dpg.get_alias_id("log_tab"))
    elif result == "success":
        dpg.bind_item_theme("progress_bar", "progress_success")
        dpg.set_value("progress_bar", 1.0)
    elif result == "error":
        dpg.bind_item_theme("progress_bar", "progress_error")
        dpg.set_value("progress_bar", 1.0)
    else:
        dpg.hide_item("progress_bar")

def _section(label: str) -> None:
    dpg.add_spacer(height=1)
    dpg.add_separator()
    t = dpg.add_text(label)
    dpg.bind_item_theme(t, "header_theme")


# ---------------------------------------------------------------------------
# Main action
# ---------------------------------------------------------------------------

def _confirm_usb_write(device: str, on_confirm: "Callable[[], None]") -> None:
    """Show an in-GUI confirmation before a destructive USB write.

    Replaces the CLI input() prompt (which would otherwise surface in a
    separate console window). On confirm, the caller runs the write with
    force=True so the underlying code skips its own console prompt.
    """
    if dpg.does_item_exist("usb_confirm_modal"):
        dpg.delete_item("usb_confirm_modal")

    def _yes() -> None:
        dpg.delete_item("usb_confirm_modal")
        on_confirm()

    vw = dpg.get_viewport_client_width() or 900
    # autosize so the window grows to fit the text + buttons — a fixed width
    # with no height gave DPG a too-short default that clipped the buttons.
    with dpg.window(label="Confirm USB write", modal=True, no_collapse=True,
                    autosize=True, tag="usb_confirm_modal",
                    pos=[max(0, vw // 2 - 235), 180]):
        dpg.add_text(f"ALL DATA on {device}", wrap=450)
        dpg.add_text("will be PERMANENTLY ERASED. This cannot be undone.",
                     wrap=450)
        dpg.add_spacer(height=10)
        with dpg.group(horizontal=True):
            dpg.add_button(label="Cancel", width=150,
                           callback=lambda: dpg.delete_item("usb_confirm_modal"))
            dpg.add_button(label="Write to USB", width=200, callback=_yes)


def _do_create() -> None:
    source_mode = dpg.get_value("source_mode")
    target_mode = dpg.get_value("target_mode")

    if source_mode == "USB drive (clone)":
        sel = dpg.get_value("source_drive_combo")
        if not sel or "no USB" in sel:
            _log("Error: No source USB drive selected.")
            return
        source = sel.split()[0]
    else:
        source = dpg.get_value("source_path").strip()
        if not source:
            _log("Error: Source directory is required.")
            return

    includes = _get_includes() if source_mode == "Folder / image file" else []
    label = dpg.get_value("vol_label").strip() or "UEFITOOLS"

    if target_mode == "File":
        output = dpg.get_value("output_path").strip()
        if not output:
            _log("Error: Output file is required.")
            return
    else:
        output = ""

    to_usb = target_mode == "USB"

    # Read options from Options tab
    partition_scheme = dpg.get_value("partition_radio")
    is_gpt = partition_scheme == "GPT"
    is_mbr = partition_scheme == "MBR"
    partitions = _get_partitions()

    # Add persistent partition if checked
    if dpg.get_value("persistent_check"):
        ps = dpg.get_value("persistent_size").strip() or "4G"
        partitions.append(PartitionSpec("ext4", ps, "casper-rw"))
        is_gpt = True

    def run(force_usb: bool = False) -> None:
        nonlocal output
        cfg = Config(
            verbose=dpg.get_value("verbose_check"),
            verify=dpg.get_value("verify_check"),
            gpt=is_gpt,
            mbr=is_mbr,
            label=label,
            force=dpg.get_value("force_check") or force_usb,
            log=_log,
            iso_hybrid=dpg.get_value("hybrid_check"),
            udf_bridge=dpg.get_value("udf_bridge_check"),
            partitions=partitions,
        )
        try:
            if not Path(source).is_dir() and Path(source).is_file():
                if not to_usb:
                    _log("Error: Image source can only target USB.")
                    _set_building(False, "error")
                    return
                _set_status("Writing image to USB...")
                _log(f"Writing {source} to USB...")
                _write_usb_from_image(cfg, source, "usb")
                _log("Done.")
                _set_status("Complete")
                _set_building(False, "success")
                return

            _set_status("Collecting files...")
            _log(f"Collecting files from {source}...")
            files = collect_files(cfg, source, includes)
            if not files:
                _log("Error: no files found.")
                _set_building(False, "error")
                return
            _log(f"  {len(files)} files")

            if to_usb:
                _set_status("Writing to USB...")
                _write_usb_from_dir(cfg, files, "usb", source, includes)
            else:
                compressed = _is_compressed_path(output)
                build_target = _strip_compression_ext(output) if compressed else output
                ext = Path(build_target).suffix.lower()
                # Use Format radio when extension is ambiguous
                if ext not in (".img", ".iso"):
                    fmt = dpg.get_value("fmt_radio")
                    is_img = "img" in fmt.lower() if fmt else True
                    # Auto-append extension if missing
                    new_ext = ".img" if is_img else ".iso"
                    build_target += new_ext
                    if compressed:
                        output = build_target + Path(output).suffix
                    else:
                        output = build_target
                    dpg.set_value("output_path", output)
                else:
                    is_img = ext == ".img"

                if is_img and cfg.gpt:
                    _set_status("Building GPT image...")
                    _log("Building GPT image...")
                    build_gpt_img(cfg, files, build_target)
                elif is_img and cfg.mbr:
                    _set_status("Building MBR image...")
                    _log("Building MBR image...")
                    from mkimage import build_mbr_img
                    build_mbr_img(cfg, files, build_target)
                elif is_img:
                    _set_status("Building image...")
                    _log("Building image...")
                    build_img(cfg, files, build_target)
                else:
                    _set_status("Building ISO image...")
                    _log("Building ISO image...")
                    build_iso(cfg, files, build_target)

                if compressed:
                    _compress_file(cfg, build_target, output)
                    os.unlink(build_target)

            _log("Done.")
            _set_status("Complete")
            _set_building(False, "success")
        except Exception as e:
            _log(f"Error: {e}")
            _set_status(f"Error: {e}")
            _set_building(False, "error")

    def _launch(force_usb: bool = False) -> None:
        _set_building(True)
        dpg.set_value("log_text", "")
        threading.Thread(target=run, args=(force_usb,), daemon=True).start()

    # Destructive USB writes get an in-GUI confirmation (no console prompt).
    # The "Force" checkbox skips it for power users.
    if to_usb and not dpg.get_value("force_check"):
        device = (dpg.get_value("drive_combo") or "").strip()
        if not device or "Refresh" in device or "no USB" in device:
            device = "the auto-detected USB drive"
        _confirm_usb_write(device, lambda: _launch(force_usb=True))
        return
    _launch()


# ---------------------------------------------------------------------------
# Progress animation
# ---------------------------------------------------------------------------

_progress_phase = 0.0

def _animate_progress() -> None:
    global _progress_phase
    if _building:
        _progress_phase = (_progress_phase + 0.008) % 1.0
        val = 0.5 + 0.3 * math.sin(_progress_phase * math.pi * 2)
        dpg.set_value("progress_bar", val)


# ---------------------------------------------------------------------------
# Keyboard navigation
# ---------------------------------------------------------------------------

def _select_tab(tab_tag: str) -> None:
    """Switch the main tab bar to the given tab."""
    try:
        dpg.set_value("tab_bar", dpg.get_alias_id(tab_tag))
    except Exception:
        pass


# Function-key shortcuts: tab navigation (F1-F5), Tools actions (F6-F9), and
# the Build action (F12). F-keys don't collide with text entry, and the
# destructive Tools actions still go through their own confirmation. This
# makes the GUI fully drivable from the keyboard — and lets automated tests
# inject keystrokes deterministically instead of hunting mouse coordinates.
_KEY_SHORTCUTS = [
    ("F1", lambda: _select_tab("build_tab")),
    ("F2", lambda: _select_tab("options_tab")),
    ("F3", lambda: _select_tab("tools_tab")),
    ("F4", lambda: _select_tab("log_tab")),
    ("F5", lambda: _select_tab("help_tab")),
    ("F6", _refresh_tools_drives),
    ("F7", _tools_format_drive),
    ("F8", _tools_wipe_drive),
    ("F9", _tools_check_drive),
    ("F12", _do_create),
]


def _setup_key_handlers() -> None:
    """Register the function-key shortcuts in a global handler registry."""
    with dpg.handler_registry():
        for keyname, action in _KEY_SHORTCUTS:
            key = getattr(dpg, f"mvKey_{keyname}", None)
            if key is None:
                continue
            # Variadic: DPG may invoke the callback with 0-3 positional args.
            dpg.add_key_press_handler(
                key, callback=lambda *_a, fn=action: fn())


# ---------------------------------------------------------------------------
# GUI entry point
# ---------------------------------------------------------------------------

def gui_main() -> None:
    dpg.create_context()
    dpg.create_viewport(title="mkimage - Bootable Media Creator",
                        width=800, height=620)
    _create_themes()

    # --- Main window with tabs ---
    with dpg.window(tag="main"):
        with dpg.tab_bar(tag="tab_bar"):

            # ===================== BUILD TAB =====================
            with dpg.tab(label="Build", tag="build_tab"):
                with dpg.group(horizontal=True):
                    # === SOURCE PANEL ===
                    with dpg.child_window(width=350, height=250, border=True):
                        t = dpg.add_text("Source")
                        dpg.bind_item_theme(t, "header_theme")

                        dpg.add_radio_button(
                            ["Folder / image file", "USB drive (clone)"], tag="source_mode",
                            horizontal=True, default_value="Folder / image file",
                            callback=_on_source_mode_change)

                        # File mode widgets
                        with dpg.group(tag="source_file_group"):
                            with dpg.group(horizontal=True):
                                dpg.add_button(
                                    label="Browse...", width=75,
                                    callback=_browse_source)
                            dpg.add_input_text(
                                tag="source_path", width=-1,
                                hint="Dir, .img, .iso, /dev/sdX")

                        # USB mode widgets (hidden)
                        with dpg.group(tag="source_usb_group", show=False):
                            with dpg.group(horizontal=True):
                                dpg.add_combo(
                                    tag="source_drive_combo",
                                    items=["(click Refresh)"], width=-80)
                                dpg.add_button(
                                    label="Refresh",
                                    callback=_refresh_source_drives, width=70)

                        # Extra includes (File mode only)
                        with dpg.group(tag="source_includes_group"):
                            dpg.add_spacer(height=2)
                            dpg.add_text("Additional Includes:")
                            with dpg.group(horizontal=True):
                                dpg.add_button(
                                    label="Add File", width=70,
                                    callback=_browse_include_file)
                                dpg.add_button(
                                    label="Add Dir", width=70,
                                    callback=_browse_include_dir)
                                dpg.add_button(
                                    label="Clear", width=40,
                                    callback=lambda: dpg.delete_item(
                                        "includes_list", children_only=True))
                            with dpg.child_window(tag="includes_list", height=40,
                                                  border=True):
                                pass

                    # === ARROW (drawn) ===
                    with dpg.drawlist(width=36, height=250):
                        # Shaft
                        dpg.draw_line([5, 125], [18, 125],
                                      color=_ACCENT, thickness=3)
                        # Arrowhead
                        dpg.draw_triangle(
                            [18, 113], [18, 137], [33, 125],
                            color=_ACCENT, fill=_ACCENT)

                    # === TARGET PANEL ===
                    with dpg.child_window(width=350, height=250, border=True):
                        t = dpg.add_text("Destination")
                        dpg.bind_item_theme(t, "header_theme")

                        dpg.add_radio_button(
                            ["File", "USB"], tag="target_mode",
                            horizontal=True, default_value="File",
                            callback=_on_target_mode_change)

                        # File mode
                        with dpg.group(tag="target_file_group"):
                            with dpg.group(horizontal=True):
                                dpg.add_button(
                                    label="Browse...", width=75,
                                    callback=_browse_output)
                            dpg.add_input_text(
                                tag="output_path", width=-1,
                                hint="Output .img, .iso, .img.gz")
                            dpg.add_spacer(height=2)
                            with dpg.group(horizontal=True):
                                dpg.add_text("Format:")
                                dpg.add_radio_button(
                                    ["Image (.img)", "ISO (.iso)"],
                                    tag="fmt_radio", horizontal=True,
                                    default_value="Image (.img)",
                                    callback=_on_fmt_change)
                            with dpg.group(horizontal=True):
                                dpg.add_text("Volume Label:")
                                dpg.add_input_text(
                                    tag="vol_label",
                                    default_value="UEFITOOLS", width=90)
                                dpg.add_text("Extra (MB):")
                                dpg.add_input_text(
                                    tag="extra_space",
                                    default_value="32", width=40)

                        # USB mode (hidden)
                        with dpg.group(tag="target_usb_group", show=False):
                            with dpg.group(horizontal=True):
                                dpg.add_combo(
                                    tag="drive_combo",
                                    items=["(click Refresh)"], width=-205)
                                dpg.add_button(
                                    label="Refresh",
                                    callback=_refresh_drives, width=70)
                                dpg.add_button(
                                    label="Check",
                                    callback=_check_drive, width=55)
                                dpg.add_button(
                                    label="Wipe",
                                    callback=_wipe_drive, width=45)
                            with dpg.group(horizontal=True):
                                dpg.add_checkbox(
                                    label="Persistent", tag="persistent_check")
                                dpg.add_input_text(
                                    tag="persistent_size",
                                    default_value="4G", width=45)

                dpg.add_spacer(height=5)
                with dpg.group(horizontal=True):
                    btn = dpg.add_button(label="Create Image", tag="action_btn",
                                         callback=_do_create, width=-70, height=36)
                    dpg.bind_item_theme(btn, "action_theme")
                    dpg.add_button(label="Exit", width=60, height=36,
                                   callback=lambda: dpg.stop_dearpygui())

            # ===================== OPTIONS TAB =====================
            with dpg.tab(label="Options", tag="options_tab"):
                _section("Partition Scheme")
                part_rb = dpg.add_radio_button(
                    ["None", "MBR", "GPT"], tag="partition_radio",
                    horizontal=True, default_value="MBR",
                    callback=_on_partition_scheme_change)
                with dpg.tooltip(part_rb):
                    dpg.add_text("None=raw filesystem, MBR=legacy boot, GPT=UEFI boot")

                _section("Partitions")
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Add Partition",
                                   callback=_add_partition_row)
                    dpg.add_button(label="Remove Last",
                                   callback=_remove_partition_row)

                with dpg.child_window(tag="partition_list", height=120,
                                      border=True):
                    pass  # rows added dynamically

                _section("ISO")
                c = dpg.add_checkbox(label="Hybrid ISO (dd-writable to USB)", tag="hybrid_check")
                with dpg.tooltip(c):
                    dpg.add_text("Embed EFI boot image so the ISO can be\nwritten directly to USB with dd")
                c = dpg.add_checkbox(label="UDF Bridge (ISO 9660 + UDF, >4GB files)", tag="udf_bridge_check")
                with dpg.tooltip(c):
                    dpg.add_text("Create dual ISO 9660 + UDF filesystem.\nSupports files larger than 4GB.")

                _section("Build Options")
                c = dpg.add_checkbox(label="Verify (SHA256 after build)", tag="verify_check")
                with dpg.tooltip(c):
                    dpg.add_text("Compare file hashes after writing")
                c = dpg.add_checkbox(label="Verbose output", tag="verbose_check")
                with dpg.tooltip(c):
                    dpg.add_text("Show detailed per-file output")
                c = dpg.add_checkbox(label="Force (skip USB confirmation)", tag="force_check")
                with dpg.tooltip(c):
                    dpg.add_text("Skip the confirmation prompt when writing to USB")

            # ===================== TOOLS TAB =====================
            with dpg.tab(label="Tools", tag="tools_tab"):
                _section("Format Drive")
                dpg.add_text("Format a USB drive with partition table and filesystem.",
                             color=(170, 170, 170))
                with dpg.group(horizontal=True):
                    dpg.add_combo(
                        tag="tools_drive_combo",
                        items=["(click Refresh)"], width=-80)
                    dpg.add_button(
                        label="Refresh", width=70,
                        callback=lambda: _refresh_tools_drives())
                with dpg.group(horizontal=True):
                    dpg.add_text("Scheme:")
                    dpg.add_radio_button(
                        ["None", "MBR", "GPT"],
                        tag="tools_scheme_radio",
                        horizontal=True, default_value="MBR")
                with dpg.group(horizontal=True):
                    dpg.add_text("FS:")
                    dpg.add_combo(
                        ["fat32", "exfat", "ntfs", "ext4"],
                        tag="tools_fs_combo",
                        default_value="fat32", width=80)
                    dpg.add_text("Label:")
                    dpg.add_input_text(
                        tag="tools_label",
                        default_value="UEFITOOLS", width=100)
                dpg.add_button(
                    label="Format", width=100,
                    callback=lambda: _tools_format_drive())
                dpg.add_spacer(height=5)

                _section("Wipe Drive")
                dpg.add_text("Remove all partition signatures from a USB drive.",
                             color=(170, 170, 170))
                dpg.add_button(
                    label="Wipe", width=100,
                    callback=lambda: _tools_wipe_drive())
                dpg.add_spacer(height=5)

                _section("Check Drive")
                dpg.add_text("Test USB drive for bad blocks (destructive).",
                             color=(170, 170, 170))
                dpg.add_button(
                    label="Check", width=100,
                    callback=lambda: _tools_check_drive())
                dpg.add_spacer(height=5)

                _section("List Image Contents")
                with dpg.group(horizontal=True):
                    dpg.add_button(
                        label="Browse...", width=75,
                        callback=lambda: _browse_list_image())
                    dpg.add_input_text(
                        tag="tools_image_path", width=-1,
                        hint="Path to .img file")
                dpg.add_button(
                    label="List", width=100,
                    callback=lambda: _tools_list_image())

            # ===================== LOG TAB =====================
            with dpg.tab(label="Log", tag="log_tab"):
                with dpg.child_window(tag="log_child", height=-1, border=True):
                    dpg.add_input_text(tag="log_text", multiline=True,
                                       readonly=True, width=-1, height=-1,
                                       default_value="", tracked=True)
                dpg.bind_item_theme(dpg.last_container(), "log_theme")

            # ===================== HELP TAB =====================
            with dpg.tab(label="Help", tag="help_tab"):
                with dpg.child_window(height=-1, border=False):
                    # Keyboard shortcuts
                    t = dpg.add_text("Keyboard Shortcuts")
                    dpg.bind_item_theme(t, "header_theme")
                    dpg.add_text("F1-F5         Build / Options / Tools / Log / Help tabs")
                    dpg.add_text("F6            Tools: refresh USB drive list")
                    dpg.add_text("F7 / F8 / F9  Tools: Format / Wipe / Check drive")
                    dpg.add_text("F12           Create Image / Write to USB")

                    dpg.add_spacer(height=1)
                    dpg.add_separator()

                    # Quick Start
                    t = dpg.add_text("Quick Start")
                    dpg.bind_item_theme(t, "header_theme")
                    dpg.add_text("1. Select a source directory containing your files")
                    dpg.add_text("2. Choose output format (Image or ISO)")
                    dpg.add_text("3. Set a target file path or select USB Drive")
                    dpg.add_text('4. Click "Create Image" or "Write to USB"')

                    dpg.add_spacer(height=1)
                    dpg.add_separator()

                    # Options Reference
                    t = dpg.add_text("Options Reference")
                    dpg.bind_item_theme(t, "header_theme")
                    dpg.add_text("Partition     None (raw), MBR (legacy BIOS), GPT (UEFI boot)")
                    dpg.add_text("ISO Hybrid    Makes ISO dd-writable to USB")
                    dpg.add_text("Verify        SHA256 check after build")
                    dpg.add_text("Verbose       Show per-file output")
                    dpg.add_text("Force         Skip USB confirmation prompt")
                    dpg.add_spacer(height=4)
                    dpg.add_text("Partition Spec (CLI: --partition TYPE:SIZE:LABEL[:DIR]):")
                    dpg.add_text("  TYPE:  esp, fat32, exfat, ntfs")
                    dpg.add_text("  SIZE:  64M (fixed), +32M (content + extra),")
                    dpg.add_text("         0 (rest of disk), empty (auto)")
                    dpg.add_text("  LABEL: volume label (11 chars max)")
                    dpg.add_text("  DIR:   optional source directory")

                    dpg.add_spacer(height=1)
                    dpg.add_separator()

                    # Tips
                    t = dpg.add_text("Tips")
                    dpg.bind_item_theme(t, "header_theme")
                    dpg.add_text("- ISO files auto-extract to bootable USB (non-hybrid)")
                    dpg.add_text("- Use .img.gz extension for compressed output")
                    dpg.add_text("- Works natively on Windows (no WSL needed)")
                    dpg.add_text("- FAT32 images don't need root; GPT/MBR do")
                    dpg.add_text("- Check Drive button tests USB for bad blocks")
                    dpg.add_text("- Persistent checkbox adds ext4 partition for live Linux")
                    dpg.add_text("- --modify flag (CLI only) edits images without rebuild")
                    dpg.add_text("- Volume labels are limited to 11 characters for FAT32")
                    dpg.add_text("- Clone USB drives: use /dev/sdX or 'usb' as source")

                    dpg.add_spacer(height=1)
                    dpg.add_separator()

                    # About
                    t = dpg.add_text("About")
                    dpg.bind_item_theme(t, "header_theme")
                    dpg.add_text("mkimage - Bootable Media Creator")
                    dpg.add_text("Cross-platform tool for creating UEFI boot images,")
                    dpg.add_text("ISOs, and USB drives.")
                    dpg.add_spacer(height=4)
                    dpg.add_text("https://github.com/mgosha/mkimage")

        # --- Status bar (below tabs, always visible) ---
        pb = dpg.add_progress_bar(tag="progress_bar", default_value=0.0,
                                  width=-1, show=False)
        dpg.bind_item_theme(pb, "progress_theme")
        st = dpg.add_text("Ready", tag="status_text")
        dpg.bind_item_theme(st, "status_theme")

    # Populate default partition row (None scheme = one fat32 row)
    _on_partition_scheme_change()

    dpg.bind_theme("main_theme")
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("main", True)
    _setup_key_handlers()

    while dpg.is_dearpygui_running():
        _flush_log()
        _animate_progress()
        dpg.render_dearpygui_frame()

    dpg.destroy_context()


if __name__ == "__main__":
    gui_main()
