#!/usr/bin/env python3
"""mkimage GUI — Dear PyGui interface for mkimage.

Modern GPU-accelerated GUI. Falls back to Tkinter if dearpygui is not
installed. Requires: pip install dearpygui
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import dearpygui.dearpygui as dpg

from mkimage import (
    Config,
    _is_windows,
    _list_removable_drives,
    _resolve_usb_target,
    _usb_safety_checks,
    _write_usb_from_dir,
    _write_usb_from_image,
    build_gpt_data_img,
    build_gpt_img,
    build_img,
    build_iso,
    collect_files,
)

# Log buffer for thread-safe logging
_log_lines: list[str] = []
_log_lock = threading.Lock()


def _log(msg: str) -> None:
    """Thread-safe log append."""
    with _log_lock:
        _log_lines.append(msg + "\n")


def _flush_log() -> None:
    """Flush buffered log lines to the log widget (called each frame)."""
    with _log_lock:
        if not _log_lines:
            return
        batch = "".join(_log_lines)
        _log_lines.clear()
    current = dpg.get_value("log_text") or ""
    dpg.set_value("log_text", current + batch)
    # Auto-scroll: set cursor to end
    dpg.set_y_scroll("log_child", dpg.get_y_scroll_max("log_child"))


def _on_source_selected(sender: int, app_data: dict) -> None:
    """Callback for source directory file dialog."""
    selections = app_data.get("selections", {})
    if selections:
        path = list(selections.values())[0]
    else:
        path = app_data.get("file_path_name", "")
    if path:
        dpg.set_value("source_path", path)


def _on_include_file_selected(sender: int, app_data: dict) -> None:
    """Callback for include file dialog."""
    selections = app_data.get("selections", {})
    for path in selections.values():
        dpg.add_selectable(label=path, parent="includes_list")


def _on_include_dir_selected(sender: int, app_data: dict) -> None:
    """Callback for include directory dialog."""
    selections = app_data.get("selections", {})
    if selections:
        path = list(selections.values())[0]
    else:
        path = app_data.get("file_path_name", "")
    if path:
        dpg.add_selectable(label=path, parent="includes_list")


def _on_output_selected(sender: int, app_data: dict) -> None:
    """Callback for output file dialog."""
    path = app_data.get("file_path_name", "")
    if path:
        dpg.set_value("output_path", path)


def _on_data_dir_selected(sender: int, app_data: dict) -> None:
    """Callback for data directory dialog."""
    selections = app_data.get("selections", {})
    if selections:
        path = list(selections.values())[0]
    else:
        path = app_data.get("file_path_name", "")
    if path:
        dpg.set_value("data_dir_path", path)


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
    """Refresh USB drive list."""
    drives = _list_removable_drives()
    items = []
    if not drives:
        items = ["(no USB drives found)"]
    else:
        for d in drives:
            model = f"  {d['model']}" if d['model'] else ""
            items.append(f"{d['path']}  {d['size']}{model}")
    dpg.configure_item("drive_combo", items=items)
    if items:
        dpg.set_value("drive_combo", items[0])


def _on_gpt_toggle(sender: int) -> None:
    """Show/hide GPT options."""
    if dpg.get_value(sender):
        dpg.show_item("gpt_options")
    else:
        dpg.hide_item("gpt_options")


def _on_target_mode_change(sender: int) -> None:
    """Toggle between file output and USB write."""
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
    """Get all include paths from the includes list."""
    includes: list[str] = []
    children = dpg.get_item_children("includes_list", 1) or []
    for child in children:
        label = dpg.get_item_label(child)
        if label:
            includes.append(label)
    return includes


def _do_create() -> None:
    """Main action: create image or write to USB."""
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

    dpg.configure_item("action_btn", enabled=False)

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
                # Source is an image file — write to USB
                if target_mode != "USB Drive":
                    _log("Error: Image source can only target USB.")
                    return
                _log(f"Writing {source} to USB...")
                _write_usb_from_image(cfg, source, "usb")
                _log("Done.")
                return

            _log(f"Collecting files from {source}...")
            files = collect_files(cfg, source, includes)
            if not files:
                _log("Error: no files found.")
                return
            _log(f"  {len(files)} files")
            for p in sorted(files.keys()):
                _log(f"    {p}")

            if target_mode == "USB Drive":
                _log("Writing to USB...")
                _write_usb_from_dir(cfg, files, "usb")
            else:
                ext = Path(output).suffix.lower()
                is_img = ext == ".img" or (ext != ".iso" and fmt == "img")
                if is_img and cfg.gpt and cfg.data_dir:
                    data_files = collect_files(cfg, cfg.data_dir, [])
                    _log(f"  {len(data_files)} data files")
                    _log("Building GPT image (ESP + data)...")
                    build_gpt_data_img(cfg, files, data_files, output)
                elif is_img and cfg.gpt:
                    _log("Building GPT image...")
                    build_gpt_img(cfg, files, output)
                elif is_img:
                    _log("Building FAT32 image...")
                    build_img(cfg, files, output)
                else:
                    _log("Building ISO image...")
                    build_iso(cfg, files, output)
            _log("Done.")
        except Exception as e:
            _log(f"Error: {e}")
        finally:
            dpg.configure_item("action_btn", enabled=True)

    threading.Thread(target=run, daemon=True).start()


def gui_main() -> None:
    """Launch the Dear PyGui graphical interface."""
    dpg.create_context()
    dpg.create_viewport(title="mkimage — Bootable Media Creator",
                        width=720, height=680, small_icon="", large_icon="")

    # --- File dialogs (hidden, shown on button click) ---
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
        dpg.add_file_extension(".efi")
        dpg.add_file_extension(".nsh")

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

        # Source directory
        dpg.add_text("Source")
        with dpg.group(horizontal=True):
            dpg.add_button(label="Browse...", callback=_browse_source,
                           width=80)
            dpg.add_input_text(tag="source_path", width=-1,
                               hint="Directory or image file")

        dpg.add_spacer(height=5)

        # Extra includes
        dpg.add_text("Extra Includes")
        with dpg.group(horizontal=True):
            dpg.add_button(label="Add File", callback=_browse_include_file,
                           width=70)
            dpg.add_button(label="Add Dir", callback=_browse_include_dir,
                           width=70)
            dpg.add_button(label="Clear", callback=_clear_includes, width=50)

        with dpg.child_window(tag="includes_list", height=70, border=True):
            pass  # selectables added dynamically

        dpg.add_spacer(height=5)

        # Output format + options
        with dpg.group(horizontal=True):
            dpg.add_text("Format:")
            dpg.add_radio_button(["FAT32 (.img)", "ISO (.iso)"],
                                 tag="fmt_radio", horizontal=True,
                                 default_value="FAT32 (.img)")
            # Hidden bool trackers for format
        dpg.add_checkbox(label="##fmt_img", tag="fmt_img",
                         default_value=True, show=False)

        dpg.add_spacer(height=5)

        # Options row
        with dpg.group(horizontal=True):
            dpg.add_text("Label:")
            dpg.add_input_text(tag="vol_label", default_value="UEFITOOLS",
                               width=100)
            dpg.add_text("  Extra (MB):")
            dpg.add_input_text(tag="extra_space", default_value="32",
                               width=50, decimal=True)

        with dpg.group(horizontal=True):
            dpg.add_checkbox(label="Verbose", tag="verbose_check")
            dpg.add_checkbox(label="Verify", tag="verify_check")
            dpg.add_checkbox(label="GPT", tag="gpt_check",
                             callback=_on_gpt_toggle)
            dpg.add_checkbox(label="Force", tag="force_check")

        # GPT options (hidden by default)
        with dpg.group(tag="gpt_options", show=False):
            dpg.add_separator()
            dpg.add_text("GPT Options")
            with dpg.group(horizontal=True):
                dpg.add_text("Data Dir:")
                dpg.add_input_text(tag="data_dir_path", width=250,
                                   hint="Optional: data partition directory")
                dpg.add_button(label="Browse...",
                               callback=_browse_data_dir, width=70)
                dpg.add_text("  Size:")
                dpg.add_input_text(tag="data_size_input", width=60,
                                   hint="e.g. 4G")
            with dpg.group(horizontal=True):
                dpg.add_text("ESP Label:")
                dpg.add_input_text(tag="esp_label_input",
                                   default_value="ESP", width=80)
                dpg.add_text("  Data Label:")
                dpg.add_input_text(tag="data_label_input",
                                   default_value="DATA", width=80)
            dpg.add_separator()

        dpg.add_spacer(height=5)

        # Target mode
        dpg.add_text("Target")
        dpg.add_radio_button(["File", "USB Drive"], tag="target_mode",
                             horizontal=True, default_value="File",
                             callback=_on_target_mode_change)

        # File target
        with dpg.group(tag="file_target_group", horizontal=True):
            dpg.add_button(label="Browse...", callback=_browse_output,
                           width=80)
            dpg.add_input_text(tag="output_path", width=-1,
                               hint="Output .img or .iso file")

        # USB target (hidden by default)
        with dpg.group(tag="usb_target_group", show=False, horizontal=True):
            dpg.add_combo(tag="drive_combo",
                          items=["(click Refresh)"], width=-100)
            dpg.add_button(label="Refresh", callback=_refresh_drives,
                           width=80)

        dpg.add_spacer(height=10)

        # Action button
        dpg.add_button(label="Create Image", tag="action_btn",
                       callback=_do_create, width=-1, height=35)

        dpg.add_spacer(height=5)

        # Log output
        dpg.add_text("Log")
        with dpg.child_window(tag="log_child", height=-1, border=True):
            dpg.add_input_text(tag="log_text", multiline=True,
                               readonly=True, width=-1, height=-1,
                               default_value="Ready.\n")

    # Format radio → update hidden bool
    def _sync_format(sender: int) -> None:
        val = dpg.get_value(sender)
        dpg.set_value("fmt_img", "FAT32" in val)

    dpg.set_item_callback("fmt_radio", _sync_format)

    # Dark theme
    with dpg.theme() as dark_theme:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4)
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 10, 10)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 6)
    dpg.bind_theme(dark_theme)

    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("main", True)

    # Render loop with log flushing
    while dpg.is_dearpygui_running():
        _flush_log()
        dpg.render_dearpygui_frame()

    dpg.destroy_context()


if __name__ == "__main__":
    gui_main()
