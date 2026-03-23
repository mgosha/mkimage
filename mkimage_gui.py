#!/usr/bin/env python3
"""mkimage GUI — Tkinter interface with tabbed layout.

Fallback GUI when Dear PyGui is not installed.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
from pathlib import Path

from mkimage import (
    Config,
    _is_compressed_path,
    _is_windows,
    _list_removable_drives,
    _strip_compression_ext,
    _write_usb_from_dir,
    _write_usb_from_image,
    _compress_file,
    build_gpt_data_img,
    build_gpt_img,
    build_mbr_img,
    build_img,
    build_iso,
    collect_files,
)


def gui_main() -> None:
    """Launch the Tkinter graphical interface."""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk
    except ImportError:
        print("Error: tkinter not available.", file=sys.stderr)
        print("  Linux:   sudo dnf install python3-tkinter  (or apt: python3-tk)", file=sys.stderr)
        print("  Windows: tkinter is included with Python", file=sys.stderr)
        sys.exit(1)

    log_queue: queue.Queue[str] = queue.Queue()

    def log(msg: str) -> None:
        log_queue.put(msg + "\n")

    def poll_log() -> None:
        while not log_queue.empty():
            msg = log_queue.get_nowait()
            log_text.insert(tk.END, msg)
            log_text.see(tk.END)
        root.after(100, poll_log)

    # --- Callbacks ---
    def browse_source() -> None:
        d = filedialog.askdirectory(title="Select Source Directory")
        if d:
            source_var.set(d)

    def add_include_file() -> None:
        f = filedialog.askopenfilename(title="Select File to Include")
        if f:
            includes_list.insert(tk.END, f)

    def add_include_dir() -> None:
        d = filedialog.askdirectory(title="Select Directory to Include")
        if d:
            includes_list.insert(tk.END, d)

    def browse_output() -> None:
        f = filedialog.asksaveasfilename(
            title="Save Image As",
            filetypes=[
                ("FAT32 Image", "*.img"),
                ("ISO Image", "*.iso"),
                ("Compressed Image", "*.img.gz"),
                ("All Files", "*.*"),
            ],
        )
        if f:
            output_var.set(f)

    def browse_data_dir() -> None:
        d = filedialog.askdirectory(title="Select Data Directory")
        if d:
            data_dir_var.set(d)

    def refresh_usb_drives() -> None:
        drives = _list_removable_drives()
        usb_drives.clear()
        usb_drives.extend(drives)
        menu = drive_combo["menu"]
        menu.delete(0, tk.END)
        if not drives:
            menu.add_command(label="(no USB drives found)",
                             command=lambda: drive_var.set(""))
            drive_var.set("")
        else:
            for d in drives:
                model = f"  {d['model']}" if d['model'] else ""
                lbl = f"{d['path']}  {d['size']}{model}"
                menu.add_command(label=lbl, command=lambda v=lbl: drive_var.set(v))
            drive_var.set(f"{drives[0]['path']}  {drives[0]['size']}"
                         f"{'  ' + drives[0]['model'] if drives[0]['model'] else ''}")

    def on_gpt_toggle() -> None:
        if gpt_var.get():
            gpt_frame.grid(row=2, column=0, columnspan=4, sticky=tk.EW, padx=10, pady=2)
        else:
            gpt_frame.grid_remove()

    def on_target_mode_change(*_args: object) -> None:
        if target_mode_var.get() == "usb":
            output_frame.grid_remove()
            usb_frame.grid(row=4, column=0, columnspan=4, sticky=tk.EW, padx=10, pady=2)
            create_btn.config(text="Write to USB")
            refresh_usb_drives()
        else:
            usb_frame.grid_remove()
            output_frame.grid(row=4, column=0, columnspan=4, sticky=tk.EW, padx=10, pady=2)
            create_btn.config(text="Create Image")

    def do_create() -> None:
        src = source_var.get().strip()
        if not src:
            messagebox.showerror("Error", "Source directory is required.")
            return

        to_usb = target_mode_var.get() == "usb"
        if not to_usb:
            out = output_var.get().strip()
            if not out:
                messagebox.showerror("Error", "Output file is required.")
                return
        else:
            out = ""

        includes = list(includes_list.get(0, tk.END))
        gui_label = label_var.get().strip() or "UEFITOOLS"
        size_mb = int(size_var.get()) if size_var.get().isdigit() else 32

        create_btn.config(state=tk.DISABLED)
        notebook.select(log_tab)  # Switch to Log tab

        def run() -> None:
            gui_data_dir = data_dir_var.get().strip() if gpt_var.get() else ""
            fs_map = {"fat32": "fat32", "exfat": "exfat", "ntfs": "ntfs"}
            cfg = Config(
                verbose=verbose_var.get(),
                verify=verify_var.get(),
                gpt=gpt_var.get() or bool(gui_data_dir),
                mbr=mbr_var.get(),
                label=gui_label,
                extra_mb=size_mb,
                force=force_var.get(),
                log=log,
                data_dir=gui_data_dir,
                data_size=data_size_var.get().strip() if gpt_var.get() else "",
                esp_label=esp_label_var.get().strip() or "ESP",
                data_label=data_label_var.get().strip() or "DATA",
                iso_hybrid=hybrid_var.get(),
                fs_type=fs_map.get(fs_var.get(), "fat32"),
            )
            try:
                if not Path(src).is_dir() and Path(src).is_file():
                    if not to_usb:
                        log("Error: Image source can only target USB.")
                        return
                    log(f"Writing {src} to USB...")
                    _write_usb_from_image(cfg, src, "usb")
                    log("Done.")
                    return

                log(f"Collecting files from {src}...")
                files = collect_files(cfg, src, includes)
                if not files:
                    log("Error: no files found.")
                    return
                log(f"  {len(files)} files")

                if to_usb:
                    log("Writing to USB...")
                    _write_usb_from_dir(cfg, files, "usb")
                else:
                    compressed = _is_compressed_path(out)
                    build_target = _strip_compression_ext(out) if compressed else out
                    ext = Path(build_target).suffix.lower()
                    is_img = ext == ".img"

                    if is_img and cfg.gpt and cfg.data_dir:
                        data_files = collect_files(cfg, cfg.data_dir, [])
                        log("Building GPT image (ESP + data)...")
                        build_gpt_data_img(cfg, files, data_files, build_target)
                    elif is_img and cfg.gpt:
                        log("Building GPT image...")
                        build_gpt_img(cfg, files, build_target)
                    elif is_img and cfg.mbr:
                        log("Building MBR image...")
                        build_mbr_img(cfg, files, build_target)
                    elif is_img:
                        log("Building image...")
                        build_img(cfg, files, build_target)
                    else:
                        log("Building ISO image...")
                        build_iso(cfg, files, build_target)

                    if compressed:
                        _compress_file(cfg, build_target, out)
                        os.unlink(build_target)

                log("Done.")
            except Exception as e:
                log(f"Error: {e}")
            finally:
                create_btn.config(state=tk.NORMAL)

        threading.Thread(target=run, daemon=True).start()

    # --- Build the window ---
    root = tk.Tk()
    root.title("mkimage \u2014 Bootable Media Creator")
    root.resizable(False, False)

    pad = {"padx": 10, "pady": 2}
    usb_drives: list[dict[str, str]] = []

    notebook = ttk.Notebook(root)
    notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    # ===================== BUILD TAB =====================
    build_tab = ttk.Frame(notebook)
    notebook.add(build_tab, text="Build")

    # Source
    tk.Label(build_tab, text="Source Directory:").grid(row=0, column=0, sticky=tk.W, **pad)
    source_var = tk.StringVar()
    tk.Entry(build_tab, textvariable=source_var, width=50).grid(row=0, column=1, columnspan=2, sticky=tk.EW, **pad)
    tk.Button(build_tab, text="Browse...", command=browse_source).grid(row=0, column=3, **pad)

    # Includes
    tk.Label(build_tab, text="Extra Includes:").grid(row=1, column=0, sticky=tk.NW, **pad)
    inc_frame = tk.Frame(build_tab)
    inc_frame.grid(row=1, column=1, columnspan=2, sticky=tk.W, **pad)
    tk.Button(inc_frame, text="Add File", command=add_include_file).pack(side=tk.LEFT, padx=(0, 5))
    tk.Button(inc_frame, text="Add Dir", command=add_include_dir).pack(side=tk.LEFT, padx=(0, 5))
    tk.Button(inc_frame, text="Clear", command=lambda: includes_list.delete(0, tk.END)).pack(side=tk.LEFT)
    includes_list = tk.Listbox(build_tab, height=3, width=60)
    includes_list.grid(row=2, column=0, columnspan=4, sticky=tk.EW, padx=10, pady=2)

    # Format + label
    tk.Label(build_tab, text="Format:").grid(row=3, column=0, sticky=tk.W, **pad)
    fmt_frame = tk.Frame(build_tab)
    fmt_frame.grid(row=3, column=1, columnspan=2, sticky=tk.W, **pad)
    fmt_var = tk.StringVar(value="img")
    tk.Radiobutton(fmt_frame, text="Image (.img)", variable=fmt_var, value="img").pack(side=tk.LEFT)
    tk.Radiobutton(fmt_frame, text="ISO (.iso)", variable=fmt_var, value="iso").pack(side=tk.LEFT, padx=10)

    tk.Label(build_tab, text="Label:").grid(row=4, column=0, sticky=tk.W, **pad)
    label_var = tk.StringVar(value="UEFITOOLS")
    tk.Entry(build_tab, textvariable=label_var, width=15).grid(row=4, column=1, sticky=tk.W, **pad)
    tk.Label(build_tab, text="Extra (MB):").grid(row=4, column=2, sticky=tk.E, **pad)
    size_var = tk.StringVar(value="32")
    tk.Entry(build_tab, textvariable=size_var, width=6).grid(row=4, column=3, sticky=tk.W, **pad)

    # Target mode
    tk.Label(build_tab, text="Target:").grid(row=5, column=0, sticky=tk.W, **pad)
    target_frame = tk.Frame(build_tab)
    target_frame.grid(row=5, column=1, columnspan=2, sticky=tk.W, **pad)
    target_mode_var = tk.StringVar(value="file")
    tk.Radiobutton(target_frame, text="File", variable=target_mode_var, value="file",
                   command=on_target_mode_change).pack(side=tk.LEFT)
    tk.Radiobutton(target_frame, text="USB Drive", variable=target_mode_var, value="usb",
                   command=on_target_mode_change).pack(side=tk.LEFT, padx=10)

    # File output
    output_frame = tk.Frame(build_tab)
    output_frame.grid(row=6, column=0, columnspan=4, sticky=tk.EW, padx=10, pady=2)
    output_var = tk.StringVar()
    tk.Button(output_frame, text="Browse...", command=browse_output).pack(side=tk.LEFT, padx=(0, 5))
    tk.Entry(output_frame, textvariable=output_var, width=50).pack(side=tk.LEFT, fill=tk.X, expand=True)

    # USB output (hidden)
    usb_frame = tk.Frame(build_tab)
    drive_var = tk.StringVar(value="")
    drive_combo = tk.OptionMenu(usb_frame, drive_var, "")
    drive_combo.config(width=42, anchor=tk.W)
    drive_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
    tk.Button(usb_frame, text="Refresh", command=refresh_usb_drives).pack(side=tk.LEFT, padx=(5, 0))

    # Action button
    create_btn = tk.Button(build_tab, text="Create Image", width=25, command=do_create)
    create_btn.grid(row=7, column=0, columnspan=4, pady=10)

    # ===================== OPTIONS TAB =====================
    options_tab = ttk.Frame(notebook)
    notebook.add(options_tab, text="Options")

    # Filesystem
    tk.Label(options_tab, text="Filesystem:").grid(row=0, column=0, sticky=tk.W, **pad)
    fs_frame = tk.Frame(options_tab)
    fs_frame.grid(row=0, column=1, columnspan=3, sticky=tk.W, **pad)
    fs_var = tk.StringVar(value="fat32")
    tk.Radiobutton(fs_frame, text="FAT32", variable=fs_var, value="fat32").pack(side=tk.LEFT)
    tk.Radiobutton(fs_frame, text="exFAT", variable=fs_var, value="exfat").pack(side=tk.LEFT, padx=10)
    tk.Radiobutton(fs_frame, text="NTFS", variable=fs_var, value="ntfs").pack(side=tk.LEFT, padx=10)

    # Partition scheme
    tk.Label(options_tab, text="Partition:").grid(row=1, column=0, sticky=tk.W, **pad)
    part_frame = tk.Frame(options_tab)
    part_frame.grid(row=1, column=1, columnspan=3, sticky=tk.W, **pad)
    partition_var = tk.StringVar(value="none")
    gpt_var = tk.BooleanVar(value=False)
    mbr_var = tk.BooleanVar(value=False)

    def on_partition_change() -> None:
        val = partition_var.get()
        gpt_var.set(val == "gpt")
        mbr_var.set(val == "mbr")
        on_gpt_toggle()

    tk.Radiobutton(part_frame, text="None", variable=partition_var,
                   value="none", command=on_partition_change).pack(side=tk.LEFT)
    tk.Radiobutton(part_frame, text="MBR", variable=partition_var,
                   value="mbr", command=on_partition_change).pack(side=tk.LEFT, padx=10)
    tk.Radiobutton(part_frame, text="GPT", variable=partition_var,
                   value="gpt", command=on_partition_change).pack(side=tk.LEFT, padx=10)

    gpt_frame = tk.LabelFrame(options_tab, text="GPT Options", padx=5, pady=5)
    data_dir_var = tk.StringVar()
    data_size_var = tk.StringVar()
    esp_label_var = tk.StringVar(value="ESP")
    data_label_var = tk.StringVar(value="DATA")

    tk.Label(gpt_frame, text="Data Dir:").grid(row=0, column=0, sticky=tk.W)
    tk.Entry(gpt_frame, textvariable=data_dir_var, width=25).grid(row=0, column=1, sticky=tk.EW, padx=2)
    tk.Button(gpt_frame, text="Browse...", command=browse_data_dir).grid(row=0, column=2, padx=2)
    tk.Label(gpt_frame, text="Size:").grid(row=0, column=3, padx=(8, 0))
    tk.Entry(gpt_frame, textvariable=data_size_var, width=8).grid(row=0, column=4, padx=2)
    tk.Label(gpt_frame, text="ESP Label:").grid(row=1, column=0, sticky=tk.W)
    tk.Entry(gpt_frame, textvariable=esp_label_var, width=10).grid(row=1, column=1, sticky=tk.W, padx=2)
    tk.Label(gpt_frame, text="Data Label:").grid(row=1, column=3, padx=(8, 0))
    tk.Entry(gpt_frame, textvariable=data_label_var, width=10).grid(row=1, column=4, sticky=tk.W, padx=2)

    # ISO hybrid
    hybrid_var = tk.BooleanVar(value=False)
    tk.Checkbutton(options_tab, text="Hybrid ISO (dd-writable to USB)",
                   variable=hybrid_var).grid(row=3, column=0, columnspan=2, sticky=tk.W, **pad)

    # Build options
    ttk.Separator(options_tab, orient=tk.HORIZONTAL).grid(
        row=4, column=0, columnspan=4, sticky=tk.EW, padx=10, pady=5)

    verify_var = tk.BooleanVar(value=False)
    tk.Checkbutton(options_tab, text="Verify (SHA256 after build)",
                   variable=verify_var).grid(row=5, column=0, columnspan=2, sticky=tk.W, **pad)

    verbose_var = tk.BooleanVar(value=False)
    tk.Checkbutton(options_tab, text="Verbose output",
                   variable=verbose_var).grid(row=6, column=0, columnspan=2, sticky=tk.W, **pad)

    force_var = tk.BooleanVar(value=False)
    tk.Checkbutton(options_tab, text="Force (skip USB confirmation)",
                   variable=force_var).grid(row=7, column=0, columnspan=2, sticky=tk.W, **pad)

    # ===================== LOG TAB =====================
    log_tab = ttk.Frame(notebook)
    notebook.add(log_tab, text="Log")

    log_text = scrolledtext.ScrolledText(log_tab, height=20, width=80,
                                          state=tk.NORMAL, font=("Consolas", 9))
    log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=(5, 0))
    log_text.insert(tk.END, "Ready.\n")

    status_label = tk.Label(log_tab, text="Ready", anchor=tk.W, fg="gray")
    status_label.pack(fill=tk.X, padx=5, pady=(0, 5))

    root.after(100, poll_log)
    root.mainloop()


if __name__ == "__main__":
    gui_main()
