#!/usr/bin/env python3
"""mkimage GUI — Tkinter interface for mkimage."""
from __future__ import annotations

import os
import queue
import sys
import threading
from pathlib import Path

from mkimage import (
    Config,
    _is_windows,
    _list_removable_drives,
    build_img,
    build_iso,
    collect_files,
    write_usb,
)


def gui_main() -> None:
    """Launch the Tkinter graphical interface."""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext
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

    def clear_includes() -> None:
        includes_list.delete(0, tk.END)

    def browse_output() -> None:
        ext = ".img" if fmt_var.get() == "img" else ".iso"
        f = filedialog.asksaveasfilename(
            title="Save Image As",
            defaultextension=ext,
            filetypes=[(f"{'FAT32' if ext == '.img' else 'ISO'} Image", f"*{ext}"), ("All Files", "*.*")],
        )
        if f:
            output_var.set(f)

    def on_format_change(*_args: object) -> None:
        state = tk.NORMAL if fmt_var.get() == "img" else tk.DISABLED
        size_entry.config(state=state)

    def refresh_usb_drives() -> None:
        """Refresh the USB drive dropdown."""
        drives = _list_removable_drives()
        usb_drives.clear()
        usb_drives.extend(drives)
        menu = drive_combo["menu"]
        menu.delete(0, tk.END)
        if not drives:
            menu.add_command(label="(no USB drives found)", command=lambda: drive_var.set(""))
            drive_var.set("")
        else:
            for d in drives:
                model = f"  {d['model']}" if d['model'] else ""
                label_text = f"{d['path']}  {d['size']}{model}"
                menu.add_command(label=label_text, command=lambda v=label_text: drive_var.set(v))
            drive_var.set(f"{drives[0]['path']}  {drives[0]['size']}"
                         f"{'  ' + drives[0]['model'] if drives[0]['model'] else ''}")

    def on_usb_toggle() -> None:
        """Toggle between file output and USB drive output."""
        if usb_var.get():
            # Switch to USB mode
            output_entry.grid_remove()
            browse_btn.grid_remove()
            drive_frame.grid(row=6, column=1, columnspan=3, sticky=tk.EW, padx=10, pady=2)
            create_btn.config(text="Write to Target")
            target_label.config(text="Output Target:")
            refresh_usb_drives()
        else:
            # Switch to file mode
            drive_frame.grid_remove()
            output_entry.grid(row=6, column=1, columnspan=2, sticky=tk.EW, padx=10, pady=2)
            browse_btn.grid(row=6, column=3, padx=10, pady=2)
            create_btn.config(text="Create Image")
            target_label.config(text="Output Target:")

    def gui_confirm_write(target: dict[str, str]) -> bool:
        return messagebox.askyesno(
            "Confirm Write",
            f"WARNING: ALL DATA on {target['path']} "
            f"({target['size']} {target['model']}) WILL BE DESTROYED.\n\n"
            f"Are you sure you want to write to {target['path']}?",
            icon=messagebox.WARNING,
        )

    def do_create() -> None:
        src = source_var.get().strip()
        if not src:
            messagebox.showerror("Error", "Source directory is required.")
            return

        to_usb = usb_var.get()
        if to_usb:
            # Find the selected drive
            sel = drive_var.get().strip()
            target_drive = None
            for d in usb_drives:
                if d["path"] in sel:
                    target_drive = d
                    break
            if not target_drive:
                messagebox.showerror("Error", "No USB drive selected.\nClick Refresh if no drives appear.")
                return
        else:
            out = output_var.get().strip()
            if not out:
                messagebox.showerror("Error", "Output file is required.")
                return

        includes = list(includes_list.get(0, tk.END))
        gui_label = label_var.get().strip() or "UEFITOOLS"
        size_mb = int(size_var.get()) if size_var.get().isdigit() else 32
        fmt = fmt_var.get()

        create_btn.config(state=tk.DISABLED)

        def run() -> None:
            gui_cfg = Config(
                verbose=verbose_var.get(),
                verify=verify_var.get(),
                gpt=gpt_var.get(),
                label=gui_label,
                extra_mb=size_mb,
                log=log,
            )
            try:
                log(f"Collecting files from {src}...")
                files = collect_files(gui_cfg, src, includes)
                if not files:
                    log("Error: no files found.")
                    return
                log(f"  {len(files)} files")
                for p in sorted(files.keys()):
                    log(f"    {p}")

                if to_usb:
                    if not gui_confirm_write(target_drive):
                        log("Aborted.")
                        return

                    if _is_windows():
                        # Windows: diskpart format + copy directly, no image needed
                        log(f"Writing to {target_drive['path']}...")
                        write_usb(
                            gui_cfg,
                            "",
                            source_dir=src,
                            includes=includes,
                            select_drive=lambda _drives: target_drive,
                            confirm_write=lambda _t: True,
                        )
                    else:
                        # Linux: build temp image, then dd to USB
                        import tempfile as _tf
                        with _tf.NamedTemporaryFile(suffix=".img", delete=False) as tmp:
                            tmp_path = tmp.name
                        try:
                            log("Building FAT32 image...")
                            build_img(gui_cfg, files, tmp_path)

                            log(f"Writing to {target_drive['path']}...")
                            write_usb(
                                gui_cfg,
                                tmp_path,
                                select_drive=lambda _drives: target_drive,
                                confirm_write=lambda _t: True,
                            )
                        finally:
                            try:
                                os.unlink(tmp_path)
                            except OSError:
                                pass
                else:
                    ext = Path(out).suffix.lower()
                    if ext == ".img" or (ext != ".iso" and fmt == "img"):
                        log("Building FAT32 image...")
                        build_img(gui_cfg, files, out)
                    else:
                        log("Building ISO image...")
                        build_iso(gui_cfg, files, out)
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

    # Source directory
    tk.Label(root, text="Source Directory:").grid(row=0, column=0, sticky=tk.W, **pad)
    source_var = tk.StringVar()
    tk.Entry(root, textvariable=source_var, width=50).grid(row=0, column=1, columnspan=2, sticky=tk.EW, **pad)
    tk.Button(root, text="Browse...", command=browse_source).grid(row=0, column=3, **pad)

    # Extra includes
    tk.Label(root, text="Extra Includes:").grid(row=1, column=0, sticky=tk.NW, **pad)
    inc_btn_frame = tk.Frame(root)
    inc_btn_frame.grid(row=1, column=1, columnspan=2, sticky=tk.W, **pad)
    tk.Button(inc_btn_frame, text="Add File", command=add_include_file).pack(side=tk.LEFT, padx=(0, 5))
    tk.Button(inc_btn_frame, text="Add Dir", command=add_include_dir).pack(side=tk.LEFT, padx=(0, 5))
    tk.Button(inc_btn_frame, text="Clear", command=clear_includes).pack(side=tk.LEFT)

    includes_list = tk.Listbox(root, height=4, width=60)
    includes_list.grid(row=2, column=0, columnspan=4, sticky=tk.EW, padx=10, pady=2)

    # Output format
    tk.Label(root, text="Output Format:").grid(row=3, column=0, sticky=tk.W, **pad)
    fmt_var = tk.StringVar(value="img")
    fmt_frame = tk.Frame(root)
    fmt_frame.grid(row=3, column=1, columnspan=2, sticky=tk.W, **pad)
    tk.Radiobutton(fmt_frame, text="FAT32 (.img)", variable=fmt_var, value="img").pack(side=tk.LEFT)
    tk.Radiobutton(fmt_frame, text="ISO (.iso)", variable=fmt_var, value="iso").pack(side=tk.LEFT, padx=10)
    fmt_var.trace_add("write", on_format_change)

    # Volume label
    tk.Label(root, text="Volume Label:").grid(row=4, column=0, sticky=tk.W, **pad)
    label_var = tk.StringVar(value="UEFITOOLS")
    tk.Entry(root, textvariable=label_var, width=20).grid(row=4, column=1, sticky=tk.W, **pad)

    # Image size
    tk.Label(root, text="Extra Space (MB):").grid(row=5, column=0, sticky=tk.W, **pad)
    size_var = tk.StringVar(value="32")
    size_entry = tk.Entry(root, textvariable=size_var, width=10)
    size_entry.grid(row=5, column=1, sticky=tk.W, **pad)

    # Write to USB toggle
    usb_var = tk.BooleanVar(value=False)
    verbose_var = tk.BooleanVar(value=False)
    verify_var = tk.BooleanVar(value=False)
    gpt_var = tk.BooleanVar(value=False)
    opt_frame = tk.Frame(root)
    opt_frame.grid(row=5, column=2, columnspan=2, sticky=tk.E, **pad)
    tk.Checkbutton(opt_frame, text="Verbose", variable=verbose_var).pack(side=tk.LEFT, padx=(0, 8))
    tk.Checkbutton(opt_frame, text="Verify", variable=verify_var).pack(side=tk.LEFT, padx=(0, 8))
    tk.Checkbutton(opt_frame, text="GPT", variable=gpt_var).pack(side=tk.LEFT, padx=(0, 8))
    tk.Checkbutton(opt_frame, text="Write to USB", variable=usb_var,
                   command=on_usb_toggle).pack(side=tk.LEFT)

    # Output target — file entry (default) or drive dropdown (USB mode)
    target_label = tk.Label(root, text="Output Target:")
    target_label.grid(row=6, column=0, sticky=tk.W, **pad)

    output_var = tk.StringVar()
    output_entry = tk.Entry(root, textvariable=output_var, width=50)
    output_entry.grid(row=6, column=1, columnspan=2, sticky=tk.EW, **pad)
    browse_btn = tk.Button(root, text="Browse...", command=browse_output)
    browse_btn.grid(row=6, column=3, **pad)

    # USB drive dropdown (hidden by default)
    drive_frame = tk.Frame(root)
    drive_var = tk.StringVar(value="")
    drive_combo = tk.OptionMenu(drive_frame, drive_var, "")
    drive_combo.config(width=45, anchor=tk.W, font=("Consolas", 9))
    drive_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
    tk.Button(drive_frame, text="Refresh", command=refresh_usb_drives).pack(side=tk.LEFT, padx=(5, 0))

    # Action button (single button, label changes)
    action_frame = tk.Frame(root)
    action_frame.grid(row=7, column=0, columnspan=4, pady=10)
    create_btn = tk.Button(action_frame, text="Create Image", width=20, command=do_create)
    create_btn.pack()

    # Log
    tk.Label(root, text="Log:").grid(row=8, column=0, sticky=tk.NW, **pad)
    log_text = scrolledtext.ScrolledText(root, height=10, width=70, state=tk.NORMAL, font=("Consolas", 9))
    log_text.grid(row=9, column=0, columnspan=4, sticky=tk.EW, padx=10, pady=(0, 10))
    log_text.insert(tk.END, "Ready.\n")

    root.after(100, poll_log)
    root.mainloop()


if __name__ == "__main__":
    gui_main()
