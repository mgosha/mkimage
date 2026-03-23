#!/usr/bin/env python3
"""mkimage GUI — Dear PyGui interface for mkimage.

Modern GPU-accelerated GUI. Falls back to Tkinter if dearpygui is not
installed. Requires: pip install dearpygui
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import dearpygui.dearpygui as dpg

from mkimage import (
    Config,
    _is_windows,
    _list_removable_drives,
    _write_usb_from_dir,
    _write_usb_from_image,
    build_gpt_data_img,
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
# Themes
# ---------------------------------------------------------------------------

# Color palette
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
    """Create all GUI themes."""
    # Main theme
    with dpg.theme(tag="main_theme"):
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 5)
            dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, 5)
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 12, 10)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 8, 4)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 5)
            dpg.add_theme_style(dpg.mvStyleVar_ItemInnerSpacing, 8, 4)
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
            dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, _ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (50, 50, 65))
            dpg.add_theme_color(dpg.mvThemeCol_Header, (45, 45, 58))
            dpg.add_theme_color(dpg.mvThemeCol_Separator, _BORDER)
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, (20, 20, 25))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, (60, 60, 75))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabHovered, (80, 80, 100))
            dpg.add_theme_color(dpg.mvThemeCol_PopupBg, (35, 35, 42))
            dpg.add_theme_color(dpg.mvThemeCol_TextSelectedBg, _ACCENT_ACTIVE)

    # Action button — green accent
    with dpg.theme(tag="action_theme"):
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, _GREEN_BTN)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, _GREEN_HOVER)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, _GREEN_ACTIVE)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 6)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 12, 8)

    # Log area — console look
    with dpg.theme(tag="log_theme"):
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (18, 18, 22))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (18, 18, 22))
            dpg.add_theme_color(dpg.mvThemeCol_Text, (180, 200, 180))
            dpg.add_theme_color(dpg.mvThemeCol_Border, (40, 40, 48))
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 6, 4)

    # Section header text
    with dpg.theme(tag="header_theme"):
        with dpg.theme_component(dpg.mvText):
            dpg.add_theme_color(dpg.mvThemeCol_Text, _HEADER)

    # Status bar
    with dpg.theme(tag="status_theme"):
        with dpg.theme_component(dpg.mvText):
            dpg.add_theme_color(dpg.mvThemeCol_Text, _TEXT_DIM)

    # Progress bar
    with dpg.theme(tag="progress_theme"):
        with dpg.theme_component(dpg.mvProgressBar):
            dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram, _ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (35, 35, 42))


# ---------------------------------------------------------------------------
# File dialog callbacks
# ---------------------------------------------------------------------------

def _on_source_selected(sender: int, app_data: dict) -> None:
    selections = app_data.get("selections", {})
    path = list(selections.values())[0] if selections else app_data.get("file_path_name", "")
    if path:
        dpg.set_value("source_path", path)


def _on_include_file_selected(sender: int, app_data: dict) -> None:
    for path in app_data.get("selections", {}).values():
        dpg.add_selectable(label=path, parent="includes_list")


def _on_include_dir_selected(sender: int, app_data: dict) -> None:
    selections = app_data.get("selections", {})
    path = list(selections.values())[0] if selections else app_data.get("file_path_name", "")
    if path:
        dpg.add_selectable(label=path, parent="includes_list")


def _on_output_selected(sender: int, app_data: dict) -> None:
    path = app_data.get("file_path_name", "")
    if path:
        dpg.set_value("output_path", path)


def _on_data_dir_selected(sender: int, app_data: dict) -> None:
    selections = app_data.get("selections", {})
    path = list(selections.values())[0] if selections else app_data.get("file_path_name", "")
    if path:
        dpg.set_value("data_dir_path", path)


# ---------------------------------------------------------------------------
# UI callbacks
# ---------------------------------------------------------------------------

def _browse_source() -> None:
    dpg.show_item("source_dialog")


def _browse_include_file() -> None:
    dpg.show_item("include_file_dialog")


def _browse_include_dir() -> None:
    dpg.show_item("include_dir_dialog")


def _clear_includes() -> None:
    dpg.delete_item("includes_list", children_only=True)


def _browse_output() -> None:
    dpg.show_item("output_dialog")


def _browse_data_dir() -> None:
    dpg.show_item("data_dir_dialog")


def _refresh_drives() -> None:
    drives = _list_removable_drives()
    items: list[str] = []
    if not drives:
        items = ["(no USB drives found)"]
    else:
        for d in drives:
            model = f"  {d['model']}" if d['model'] else ""
            items.append(f"{d['path']}  {d['size']}{model}")
    dpg.configure_item("drive_combo", items=items)
    if items:
        dpg.set_value("drive_combo", items[0])
    _set_status(f"Found {len(drives)} USB drive(s)" if drives else "No USB drives found")


def _on_gpt_toggle(sender: int) -> None:
    if dpg.get_value(sender):
        dpg.show_item("gpt_options")
    else:
        dpg.hide_item("gpt_options")


def _on_target_mode_change(sender: int) -> None:
    mode = dpg.get_value(sender)
    if mode == "USB Drive":
        dpg.hide_item("file_target_group")
        dpg.show_item("usb_target_group")
        dpg.configure_item("action_btn", label="Write to USB")
        _refresh_drives()
    else:
        dpg.show_item("file_target_group")
        dpg.hide_item("usb_target_group")
        dpg.configure_item("action_btn", label="Create Image")


def _get_includes() -> list[str]:
    includes: list[str] = []
    children = dpg.get_item_children("includes_list", 1) or []
    for child in children:
        label = dpg.get_item_label(child)
        if label:
            includes.append(label)
    return includes


def _set_building(active: bool) -> None:
    """Enable/disable form during build."""
    global _building
    _building = active
    dpg.configure_item("action_btn", enabled=not active)
    if active:
        dpg.show_item("progress_bar")
        _set_status("Building...")
    else:
        dpg.hide_item("progress_bar")


# ---------------------------------------------------------------------------
# Section header helper
# ---------------------------------------------------------------------------

def _section(label: str) -> None:
    """Add a styled section header with separator."""
    dpg.add_spacer(height=1)
    dpg.add_separator()
    t = dpg.add_text(label)
    dpg.bind_item_theme(t, "header_theme")


# ---------------------------------------------------------------------------
# Main action
# ---------------------------------------------------------------------------

def _do_create() -> None:
    source = dpg.get_value("source_path").strip()
    if not source:
        _log("Error: Source directory is required.")
        return

    includes = _get_includes()
    label = dpg.get_value("vol_label").strip() or "UEFITOOLS"
    extra_str = dpg.get_value("extra_space").strip()
    extra_mb = int(extra_str) if extra_str.isdigit() else 32
    fmt = "img" if dpg.get_value("fmt_img") else "iso"
    is_gpt = dpg.get_value("gpt_check")
    data_dir = dpg.get_value("data_dir_path").strip() if is_gpt else ""
    data_size = dpg.get_value("data_size_input").strip() if is_gpt else ""
    esp_label = dpg.get_value("esp_label_input").strip() or "ESP"
    data_label = dpg.get_value("data_label_input").strip() or "DATA"
    target_mode = dpg.get_value("target_mode")

    if target_mode == "File":
        output = dpg.get_value("output_path").strip()
        if not output:
            _log("Error: Output file is required.")
            return
    else:
        output = ""

    _set_building(True)
    dpg.set_value("log_text", "")  # clear log

    def run() -> None:
        cfg = Config(
            verbose=dpg.get_value("verbose_check"),
            verify=dpg.get_value("verify_check"),
            gpt=is_gpt or bool(data_dir),
            label=label,
            extra_mb=extra_mb,
            force=dpg.get_value("force_check"),
            log=_log,
            data_dir=data_dir,
            data_size=data_size,
            esp_label=esp_label,
            data_label=data_label,
        )
        try:
            if not Path(source).is_dir() and Path(source).is_file():
                if target_mode != "USB Drive":
                    _log("Error: Image source can only target USB.")
                    return
                _set_status("Writing image to USB...")
                _log(f"Writing {source} to USB...")
                _write_usb_from_image(cfg, source, "usb")
                _log("Done.")
                _set_status("Complete")
                return

            _set_status("Collecting files...")
            _log(f"Collecting files from {source}...")
            files = collect_files(cfg, source, includes)
            if not files:
                _log("Error: no files found.")
                _set_status("Error: no files")
                return
            _log(f"  {len(files)} files")
            for p in sorted(files.keys()):
                _log(f"    {p}")

            if target_mode == "USB Drive":
                _set_status("Writing to USB...")
                _log("Writing to USB...")
                _write_usb_from_dir(cfg, files, "usb")
            else:
                ext = Path(output).suffix.lower()
                is_img = ext == ".img" or (ext != ".iso" and fmt == "img")
                if is_img and cfg.gpt and cfg.data_dir:
                    data_files = collect_files(cfg, cfg.data_dir, [])
                    _log(f"  {len(data_files)} data files")
                    _set_status("Building GPT image (ESP + data)...")
                    _log("Building GPT image (ESP + data)...")
                    build_gpt_data_img(cfg, files, data_files, output)
                elif is_img and cfg.gpt:
                    _set_status("Building GPT image...")
                    _log("Building GPT image...")
                    build_gpt_img(cfg, files, output)
                elif is_img:
                    _set_status("Building FAT32 image...")
                    _log("Building FAT32 image...")
                    build_img(cfg, files, output)
                else:
                    _set_status("Building ISO image...")
                    _log("Building ISO image...")
                    build_iso(cfg, files, output)
            _log("Done.")
            _set_status("Complete")
        except Exception as e:
            _log(f"Error: {e}")
            _set_status(f"Error: {e}")
        finally:
            _set_building(False)

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Progress animation
# ---------------------------------------------------------------------------

_progress_phase = 0.0


def _animate_progress() -> None:
    """Animate the indeterminate progress bar."""
    global _progress_phase
    if _building:
        _progress_phase = (_progress_phase + 0.008) % 1.0
        # Pulse between 0.2 and 0.8 for indeterminate feel
        import math
        val = 0.5 + 0.3 * math.sin(_progress_phase * math.pi * 2)
        dpg.set_value("progress_bar", val)


# ---------------------------------------------------------------------------
# GUI entry point
# ---------------------------------------------------------------------------

def gui_main() -> None:
    """Launch the Dear PyGui graphical interface."""
    dpg.create_context()
    dpg.create_viewport(title="mkimage — Bootable Media Creator",
                        width=720, height=750)

    _create_themes()

    # --- File dialogs ---
    with dpg.file_dialog(label="Select Source Directory",
                         callback=_on_source_selected,
                         directory_selector=True, show=False,
                         tag="source_dialog", width=600, height=400):
        dpg.add_file_extension(".*")

    with dpg.file_dialog(label="Select File to Include",
                         callback=_on_include_file_selected,
                         show=False, tag="include_file_dialog",
                         width=600, height=400):
        dpg.add_file_extension(".*")
        dpg.add_file_extension(".efi", color=(0, 255, 0))
        dpg.add_file_extension(".nsh", color=(0, 200, 255))

    with dpg.file_dialog(label="Select Directory to Include",
                         callback=_on_include_dir_selected,
                         directory_selector=True, show=False,
                         tag="include_dir_dialog", width=600, height=400):
        dpg.add_file_extension(".*")

    with dpg.file_dialog(label="Save Image As",
                         callback=_on_output_selected,
                         show=False, tag="output_dialog",
                         width=600, height=400):
        dpg.add_file_extension(".img", color=(0, 255, 0))
        dpg.add_file_extension(".iso", color=(0, 200, 255))

    with dpg.file_dialog(label="Select Data Directory",
                         callback=_on_data_dir_selected,
                         directory_selector=True, show=False,
                         tag="data_dir_dialog", width=600, height=400):
        dpg.add_file_extension(".*")

    # --- Main window ---
    with dpg.window(tag="main"):

        # --- Source ---
        _section("Source")
        with dpg.group(horizontal=True):
            b = dpg.add_button(label="Browse...", callback=_browse_source, width=85)
            with dpg.tooltip(b):
                dpg.add_text("Select source directory or image file")
            dpg.add_input_text(tag="source_path", width=-1,
                               hint="Directory with files to include, or existing .img")

        # --- Extra Includes ---
        _section("Extra Includes")
        with dpg.group(horizontal=True):
            b1 = dpg.add_button(label="Add File", callback=_browse_include_file, width=75)
            with dpg.tooltip(b1):
                dpg.add_text("Add individual files to include")
            b2 = dpg.add_button(label="Add Dir", callback=_browse_include_dir, width=75)
            with dpg.tooltip(b2):
                dpg.add_text("Add a directory tree to include")
            b3 = dpg.add_button(label="Clear", callback=_clear_includes, width=55)
            with dpg.tooltip(b3):
                dpg.add_text("Remove all extra includes")

        with dpg.child_window(tag="includes_list", height=45, border=True):
            pass

        # --- Format & Options ---
        _section("Options")
        with dpg.group(horizontal=True):
            dpg.add_text("Format:")
            dpg.add_radio_button(["FAT32 (.img)", "ISO (.iso)"],
                                 tag="fmt_radio", horizontal=True,
                                 default_value="FAT32 (.img)")
        dpg.add_checkbox(tag="fmt_img", default_value=True, show=False)

        with dpg.group(horizontal=True):
            dpg.add_text("Label:")
            dpg.add_input_text(tag="vol_label", default_value="UEFITOOLS", width=110)
            dpg.add_spacer(width=15)
            dpg.add_text("Extra (MB):")
            dpg.add_input_text(tag="extra_space", default_value="32",
                               width=55, decimal=True)

        with dpg.group(horizontal=True):
            c1 = dpg.add_checkbox(label="Verbose", tag="verbose_check")
            with dpg.tooltip(c1):
                dpg.add_text("Show detailed per-file output")
            c2 = dpg.add_checkbox(label="Verify", tag="verify_check")
            with dpg.tooltip(c2):
                dpg.add_text("Verify files after writing to USB")
            c3 = dpg.add_checkbox(label="GPT", tag="gpt_check",
                                  callback=_on_gpt_toggle)
            with dpg.tooltip(c3):
                dpg.add_text("Create GPT partition table with EFI System Partition")
            c4 = dpg.add_checkbox(label="Force", tag="force_check")
            with dpg.tooltip(c4):
                dpg.add_text("Skip USB write confirmation prompt")

        # --- GPT Options (hidden) ---
        with dpg.group(tag="gpt_options", show=False):
            with dpg.group(horizontal=True):
                dpg.add_text("Data Dir:")
                dpg.add_input_text(tag="data_dir_path", width=220,
                                   hint="Optional: data partition")
                dpg.add_button(label="Browse...", callback=_browse_data_dir, width=70)
                dpg.add_spacer(width=8)
                dpg.add_text("Size:")
                dpg.add_input_text(tag="data_size_input", width=55, hint="e.g. 4G")
            with dpg.group(horizontal=True):
                dpg.add_text("ESP Label:")
                dpg.add_input_text(tag="esp_label_input", default_value="ESP", width=80)
                dpg.add_spacer(width=15)
                dpg.add_text("Data Label:")
                dpg.add_input_text(tag="data_label_input", default_value="DATA", width=80)

        # --- Target ---
        _section("Target")
        dpg.add_radio_button(["File", "USB Drive"], tag="target_mode",
                             horizontal=True, default_value="File",
                             callback=_on_target_mode_change)

        with dpg.group(tag="file_target_group", horizontal=True):
            b = dpg.add_button(label="Browse...", callback=_browse_output, width=85)
            with dpg.tooltip(b):
                dpg.add_text("Choose output .img or .iso file location")
            dpg.add_input_text(tag="output_path", width=-1,
                               hint="Output .img or .iso file")

        with dpg.group(tag="usb_target_group", show=False, horizontal=True):
            dpg.add_combo(tag="drive_combo", items=["(click Refresh)"], width=-100)
            b = dpg.add_button(label="Refresh", callback=_refresh_drives, width=85)
            with dpg.tooltip(b):
                dpg.add_text("Scan for removable USB drives")

        # --- Progress bar (hidden) ---
        pb = dpg.add_progress_bar(tag="progress_bar", default_value=0.0,
                                  width=-1, show=False)
        dpg.bind_item_theme(pb, "progress_theme")

        # --- Action button ---
        dpg.add_spacer(height=3)
        btn = dpg.add_button(label="Create Image", tag="action_btn",
                             callback=_do_create, width=-1, height=36)
        dpg.bind_item_theme(btn, "action_theme")

        # --- Log ---
        _section("Log")
        with dpg.child_window(tag="log_child", height=160, border=True):
            dpg.add_input_text(tag="log_text", multiline=True,
                               readonly=True, width=-1, height=-1,
                               default_value="Ready.\n", tracked=True)
        dpg.bind_item_theme(dpg.last_container(), "log_theme")

        # --- Status bar ---
        st = dpg.add_text("Ready", tag="status_text")
        dpg.bind_item_theme(st, "status_theme")

    # Format radio sync
    def _sync_format(sender: int) -> None:
        dpg.set_value("fmt_img", "FAT32" in dpg.get_value(sender))

    dpg.set_item_callback("fmt_radio", _sync_format)

    # Apply main theme
    dpg.bind_theme("main_theme")

    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("main", True)

    # Render loop
    while dpg.is_dearpygui_running():
        _flush_log()
        _animate_progress()
        dpg.render_dearpygui_frame()

    dpg.destroy_context()


if __name__ == "__main__":
    gui_main()
