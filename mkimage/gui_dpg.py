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
# File dialog callbacks
# ---------------------------------------------------------------------------

def _on_source_selected(_s: int, app_data: dict) -> None:
    sel = app_data.get("selections", {})
    path = list(sel.values())[0] if sel else app_data.get("file_path_name", "")
    if path:
        dpg.set_value("source_path", path)

def _on_include_file_selected(_s: int, app_data: dict) -> None:
    for path in app_data.get("selections", {}).values():
        dpg.add_selectable(label=path, parent="includes_list")

def _on_include_dir_selected(_s: int, app_data: dict) -> None:
    sel = app_data.get("selections", {})
    path = list(sel.values())[0] if sel else app_data.get("file_path_name", "")
    if path:
        dpg.add_selectable(label=path, parent="includes_list")

def _on_output_selected(_s: int, app_data: dict) -> None:
    path = app_data.get("file_path_name", "")
    if path:
        dpg.set_value("output_path", path)

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
    dpg.add_combo(["esp", "fat32", "exfat", "ntfs", "ext4"],
                  default_value=fs, width=70, parent=row_id)
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
            partitions.append(PartitionSpec(
                fs_type=dpg.get_value(children[0]),
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


def _on_source_mode_change(sender: int) -> None:
    mode = dpg.get_value(sender)
    if mode == "USB":
        dpg.hide_item("source_file_group")
        dpg.hide_item("source_includes_group")
        dpg.show_item("source_usb_group")
        _refresh_source_drives()
    else:
        dpg.show_item("source_file_group")
        dpg.show_item("source_includes_group")
        dpg.hide_item("source_usb_group")
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


def _update_action_label() -> None:
    src = dpg.get_value("source_mode")
    tgt = dpg.get_value("target_mode")
    if src == "File" and tgt == "File":
        label = "Create Image"
    elif src == "File" and tgt == "USB":
        label = "Write to USB"
    elif src == "USB" and tgt == "File":
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

def _do_create() -> None:
    source_mode = dpg.get_value("source_mode")
    if source_mode == "USB":
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

    includes = _get_includes() if source_mode == "File" else []
    label = dpg.get_value("vol_label").strip() or "UEFITOOLS"
    target_mode = dpg.get_value("target_mode")

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

    _set_building(True)
    dpg.set_value("log_text", "")

    def run() -> None:
        cfg = Config(
            verbose=dpg.get_value("verbose_check"),
            verify=dpg.get_value("verify_check"),
            gpt=is_gpt,
            mbr=is_mbr,
            label=label,
            force=dpg.get_value("force_check"),
            log=_log,
            iso_hybrid=dpg.get_value("hybrid_check"),
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
                _write_usb_from_dir(cfg, files, "usb")
            else:
                compressed = _is_compressed_path(output)
                build_target = _strip_compression_ext(output) if compressed else output
                ext = Path(build_target).suffix.lower()
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

    threading.Thread(target=run, daemon=True).start()


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
# GUI entry point
# ---------------------------------------------------------------------------

def gui_main() -> None:
    dpg.create_context()
    dpg.create_viewport(title="mkimage \u2014 Bootable Media Creator",
                        width=800, height=620)
    _create_themes()

    # --- File dialogs ---
    for tag, label, dir_sel, cb in [
        ("source_dialog", "Select Source", True, _on_source_selected),
        ("include_file_dialog", "Select File", False, _on_include_file_selected),
        ("include_dir_dialog", "Select Directory", True, _on_include_dir_selected),
    ]:
        with dpg.file_dialog(label=label, callback=cb, directory_selector=dir_sel,
                             show=False, tag=tag, width=600, height=400):
            dpg.add_file_extension(".*")
            if not dir_sel:
                dpg.add_file_extension(".efi", color=(0, 255, 0))
                dpg.add_file_extension(".nsh", color=(0, 200, 255))

    with dpg.file_dialog(label="Save Image As", callback=_on_output_selected,
                         show=False, tag="output_dialog", width=600, height=400):
        dpg.add_file_extension(".img", color=(0, 255, 0))
        dpg.add_file_extension(".iso", color=(0, 200, 255))
        dpg.add_file_extension(".img.gz", color=(200, 200, 0))

    # --- Main window with tabs ---
    with dpg.window(tag="main"):
        with dpg.tab_bar(tag="tab_bar"):

            # ===================== BUILD TAB =====================
            with dpg.tab(label="Build", tag="build_tab"):
                with dpg.group(horizontal=True):
                    # === SOURCE PANEL ===
                    with dpg.child_window(width=345, height=210, border=True):
                        t = dpg.add_text("Source")
                        dpg.bind_item_theme(t, "header_theme")

                        dpg.add_radio_button(
                            ["File", "USB"], tag="source_mode",
                            horizontal=True, default_value="File",
                            callback=_on_source_mode_change)

                        # File mode widgets
                        with dpg.group(tag="source_file_group"):
                            with dpg.group(horizontal=True):
                                dpg.add_button(
                                    label="Browse...", width=75,
                                    callback=lambda: dpg.show_item("source_dialog"))
                                dpg.add_button(
                                    label="USB", width=35,
                                    callback=lambda: dpg.set_value("source_path", "usb"))
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
                            with dpg.group(horizontal=True):
                                dpg.add_button(
                                    label="Add File", width=60,
                                    callback=lambda: dpg.show_item("include_file_dialog"))
                                dpg.add_button(
                                    label="Add Dir", width=60,
                                    callback=lambda: dpg.show_item("include_dir_dialog"))
                                dpg.add_button(
                                    label="Clear", width=45,
                                    callback=lambda: dpg.delete_item(
                                        "includes_list", children_only=True))
                            with dpg.child_window(tag="includes_list", height=55,
                                                  border=True):
                                pass

                    # === ARROW ===
                    dpg.add_spacer(width=5)
                    arrow = dpg.add_text("\u25ba")
                    dpg.bind_item_theme(arrow, "header_theme")
                    dpg.add_spacer(width=5)

                    # === TARGET PANEL ===
                    with dpg.child_window(width=345, height=210, border=True):
                        t = dpg.add_text("Target")
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
                                    callback=lambda: dpg.show_item("output_dialog"))
                                dpg.add_button(
                                    label="USB", width=35,
                                    callback=lambda: dpg.set_value("output_path", "usb"))
                            dpg.add_input_text(
                                tag="output_path", width=-1,
                                hint="Output .img, .iso, .img.gz")

                        # USB mode (hidden)
                        with dpg.group(tag="target_usb_group", show=False):
                            with dpg.group(horizontal=True):
                                dpg.add_combo(
                                    tag="drive_combo",
                                    items=["(click Refresh)"], width=-170)
                                dpg.add_button(
                                    label="Refresh",
                                    callback=_refresh_drives, width=70)
                                dpg.add_button(
                                    label="Check",
                                    callback=_check_drive, width=55)
                            with dpg.group(horizontal=True):
                                dpg.add_checkbox(
                                    label="Persistent", tag="persistent_check")
                                dpg.add_input_text(
                                    tag="persistent_size",
                                    default_value="4G", width=45)

                # Format + Label + Extra below both panels
                dpg.add_spacer(height=3)
                with dpg.group(horizontal=True):
                    dpg.add_text("Format:")
                    dpg.add_radio_button(
                        ["Image (.img)", "ISO (.iso)"], tag="fmt_radio",
                        horizontal=True, default_value="Image (.img)")
                with dpg.group(horizontal=True):
                    dpg.add_text("Label:")
                    dpg.add_input_text(
                        tag="vol_label", default_value="UEFITOOLS", width=100)
                    dpg.add_spacer(width=10)
                    dpg.add_text("Extra (MB):")
                    dpg.add_input_text(
                        tag="extra_space", default_value="32", width=50)

                dpg.add_spacer(height=5)
                btn = dpg.add_button(label="Create Image", tag="action_btn",
                                     callback=_do_create, width=-1, height=36)
                dpg.bind_item_theme(btn, "action_theme")

            # ===================== OPTIONS TAB =====================
            with dpg.tab(label="Options", tag="options_tab"):
                _section("Partition Scheme")
                part_rb = dpg.add_radio_button(
                    ["None", "MBR", "GPT"], tag="partition_radio",
                    horizontal=True, default_value="None",
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

    while dpg.is_dearpygui_running():
        _flush_log()
        _animate_progress()
        dpg.render_dearpygui_frame()

    dpg.destroy_context()


if __name__ == "__main__":
    gui_main()
