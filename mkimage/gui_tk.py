"""mkimage GUI -- Tkinter interface with tabbed layout.

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
    PartitionSpec,
    _is_compressed_path,
    _is_windows,
    _list_removable_drives,
    _strip_compression_ext,
    _write_usb_from_dir,
    _write_usb_from_image,
    _compress_file,
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

    class ToolTip:
        def __init__(self, widget: tk.Widget, text: str) -> None:
            self.widget = widget
            self.text = text
            widget.bind("<Enter>", self._show)
            widget.bind("<Leave>", self._hide)
            self.tip: tk.Toplevel | None = None

        def _show(self, event: tk.Event) -> None:  # type: ignore[type-arg]
            x, y = event.x_root + 15, event.y_root + 10
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")
            label = tk.Label(self.tip, text=self.text, background="#ffffe0",
                             relief="solid", borderwidth=1, font=("Segoe UI", 9))
            label.pack()

        def _hide(self, event: tk.Event) -> None:  # type: ignore[type-arg]
            if self.tip:
                self.tip.destroy()
                self.tip = None

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

    def _check_drive_tk() -> None:
        """Run bad block check on selected USB drive."""
        from mkimage.usb.safety import _check_bad_blocks, _unmount_device
        sel = drive_var.get().strip()
        if not sel:
            messagebox.showerror("Error", "No USB drive selected.")
            return
        device = sel.split()[0]
        if not messagebox.askyesno("Confirm", f"Bad block test will ERASE ALL DATA on {device}.\n\nProceed?"):
            return
        notebook.select(log_tab)
        create_btn.config(state=tk.DISABLED)

        def run() -> None:
            cfg_chk = Config(log=log, verbose=True)
            try:
                _unmount_device(cfg_chk, device)
                if _check_bad_blocks(cfg_chk, device):
                    log(f"[OK] No bad blocks found on {device}.")
                else:
                    log(f"[FAIL] Bad blocks detected on {device}!")
            except Exception as e:
                log(f"Error: {e}")
            finally:
                create_btn.config(state=tk.NORMAL)

        threading.Thread(target=run, daemon=True).start()

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

        # Read partition scheme and rows from Options tab
        is_gpt = part_scheme_var.get() == "gpt"
        is_mbr = part_scheme_var.get() == "mbr"
        partitions = get_partitions()

        # Add persistent partition if checked
        if persistent_var.get():
            ps = persistent_size_var.get().strip() or "4G"
            partitions.append(PartitionSpec("ext4", ps, "casper-rw"))
            is_gpt = True

        create_btn.config(state=tk.DISABLED)
        notebook.select(log_tab)  # Switch to Log tab

        def run() -> None:
            cfg = Config(
                verbose=verbose_var.get(),
                verify=verify_var.get(),
                gpt=is_gpt,
                mbr=is_mbr,
                label=gui_label,
                force=force_var.get(),
                log=log,
                iso_hybrid=hybrid_var.get(),
                partitions=partitions,
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

                    if is_img and cfg.gpt:
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
    tk.Label(build_tab, text="Source:").grid(row=0, column=0, sticky=tk.W, **pad)
    source_var = tk.StringVar()
    src_frame = tk.Frame(build_tab)
    src_frame.grid(row=0, column=1, columnspan=2, sticky=tk.EW, **pad)
    source_entry = tk.Entry(src_frame, textvariable=source_var, width=45)
    source_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
    ToolTip(source_entry, "Directory, image file, /dev/sdX device, or 'usb' to auto-detect")
    usb_src_btn = tk.Button(src_frame, text="USB", width=4,
                            command=lambda: source_var.set("usb"))
    usb_src_btn.pack(side=tk.LEFT, padx=(3, 0))
    ToolTip(usb_src_btn, "Auto-detect USB drive (for cloning)")
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
    ToolTip(includes_list, "Extra files/directories added to the image")

    # Format + label
    tk.Label(build_tab, text="Format:").grid(row=3, column=0, sticky=tk.W, **pad)
    fmt_frame = tk.Frame(build_tab)
    fmt_frame.grid(row=3, column=1, columnspan=2, sticky=tk.W, **pad)
    fmt_var = tk.StringVar(value="img")
    tk.Radiobutton(fmt_frame, text="Image (.img)", variable=fmt_var, value="img").pack(side=tk.LEFT)
    tk.Radiobutton(fmt_frame, text="ISO (.iso)", variable=fmt_var, value="iso").pack(side=tk.LEFT, padx=10)
    ToolTip(fmt_frame, "Image (.img) for disk images, ISO (.iso) for optical/hybrid")

    tk.Label(build_tab, text="Label:").grid(row=4, column=0, sticky=tk.W, **pad)
    label_var = tk.StringVar(value="UEFITOOLS")
    label_entry = tk.Entry(build_tab, textvariable=label_var, width=15)
    label_entry.grid(row=4, column=1, sticky=tk.W, **pad)
    ToolTip(label_entry, "Volume label (11 chars max for FAT32)")
    tk.Label(build_tab, text="Extra (MB):").grid(row=4, column=2, sticky=tk.E, **pad)
    size_var = tk.StringVar(value="32")
    extra_entry = tk.Entry(build_tab, textvariable=size_var, width=6)
    extra_entry.grid(row=4, column=3, sticky=tk.W, **pad)
    ToolTip(extra_entry, "Free space added beyond content size")

    # Target mode
    tk.Label(build_tab, text="Target:").grid(row=5, column=0, sticky=tk.W, **pad)
    target_frame = tk.Frame(build_tab)
    target_frame.grid(row=5, column=1, columnspan=2, sticky=tk.W, **pad)
    target_mode_var = tk.StringVar(value="file")
    tk.Radiobutton(target_frame, text="File", variable=target_mode_var, value="file",
                   command=on_target_mode_change).pack(side=tk.LEFT)
    tk.Radiobutton(target_frame, text="USB Drive", variable=target_mode_var, value="usb",
                   command=on_target_mode_change).pack(side=tk.LEFT, padx=10)
    ToolTip(target_frame, "File saves to disk, USB Drive writes directly")

    # File output
    output_frame = tk.Frame(build_tab)
    output_frame.grid(row=6, column=0, columnspan=4, sticky=tk.EW, padx=10, pady=2)
    output_var = tk.StringVar()
    tk.Button(output_frame, text="Browse...", command=browse_output).pack(side=tk.LEFT, padx=(0, 5))
    output_entry = tk.Entry(output_frame, textvariable=output_var, width=50)
    output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
    ToolTip(output_entry, "Use .img.gz for compressed output")

    # USB output (hidden)
    usb_frame = tk.Frame(build_tab)
    # Drive row
    usb_drive_row = tk.Frame(usb_frame)
    usb_drive_row.pack(fill=tk.X)
    drive_var = tk.StringVar(value="")
    drive_combo = tk.OptionMenu(usb_drive_row, drive_var, "")
    drive_combo.config(width=35, anchor=tk.W)
    drive_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
    ToolTip(drive_combo, "Select a removable USB drive")
    tk.Button(usb_drive_row, text="Refresh", command=refresh_usb_drives).pack(side=tk.LEFT, padx=(5, 0))
    tk.Button(usb_drive_row, text="Check Drive", command=lambda: _check_drive_tk()).pack(side=tk.LEFT, padx=(5, 0))
    # Persistent row
    usb_persist_row = tk.Frame(usb_frame)
    usb_persist_row.pack(fill=tk.X, pady=(2, 0))
    persistent_var = tk.BooleanVar(value=False)
    tk.Checkbutton(usb_persist_row, text="Persistent storage",
                   variable=persistent_var).pack(side=tk.LEFT)
    persistent_size_var = tk.StringVar(value="4G")
    tk.Entry(usb_persist_row, textvariable=persistent_size_var,
             width=6).pack(side=tk.LEFT, padx=2)
    ToolTip(usb_persist_row, "Add ext4 casper-rw partition for Linux live USBs")

    # Action button
    create_btn = tk.Button(build_tab, text="Create Image", width=25, command=do_create)
    create_btn.grid(row=7, column=0, columnspan=4, pady=10)

    # ===================== OPTIONS TAB =====================
    options_tab = ttk.Frame(notebook)
    notebook.add(options_tab, text="Options")

    # Partition scheme
    tk.Label(options_tab, text="Partition:").grid(row=0, column=0, sticky=tk.W, **pad)
    part_frame = tk.Frame(options_tab)
    part_frame.grid(row=0, column=1, columnspan=3, sticky=tk.W, **pad)
    part_scheme_var = tk.StringVar(value="none")

    partition_rows: list[tuple[tk.Frame, tk.StringVar, tk.StringVar,
                               tk.StringVar, tk.StringVar,
                               tk.StringVar]] = []

    def _browse_part_src(dir_var: tk.StringVar) -> None:
        d = filedialog.askdirectory(title="Select Source Directory")
        if d:
            dir_var.set(d)

    def add_partition_row(fs: str = "fat32", size: str = "",
                          label: str = "UEFITOOLS", src: str = "",
                          cluster: str = "") -> None:
        row = tk.Frame(partition_frame)
        type_var = tk.StringVar(value=fs)
        size_var_p = tk.StringVar(value=size)
        label_var_p = tk.StringVar(value=label)
        cluster_var = tk.StringVar(value=cluster)
        dir_var = tk.StringVar(value=src)

        ttk.Combobox(row, textvariable=type_var,
                     values=["esp", "fat32", "exfat", "ntfs", "ext4"],
                     width=7).pack(side=tk.LEFT, padx=2)
        tk.Entry(row, textvariable=size_var_p, width=7).pack(
            side=tk.LEFT, padx=2)
        tk.Entry(row, textvariable=label_var_p, width=9).pack(
            side=tk.LEFT, padx=2)
        tk.Entry(row, textvariable=cluster_var, width=6).pack(
            side=tk.LEFT, padx=2)
        tk.Entry(row, textvariable=dir_var, width=15).pack(
            side=tk.LEFT, padx=2, fill=tk.X, expand=True)
        tk.Button(row, text="...",
                  command=lambda: _browse_part_src(dir_var)).pack(
            side=tk.LEFT)

        row.pack(fill=tk.X, padx=5, pady=1)
        partition_rows.append((row, type_var, size_var_p, label_var_p,
                               cluster_var, dir_var))

    def remove_partition_row() -> None:
        if len(partition_rows) > 1:
            row, *_ = partition_rows.pop()
            row.destroy()

    def clear_partition_rows() -> None:
        for row, *_ in partition_rows:
            row.destroy()
        partition_rows.clear()

    def on_scheme_change() -> None:
        clear_partition_rows()
        val = part_scheme_var.get()
        if val == "gpt":
            add_partition_row(fs="esp", label="ESP")
        elif val == "mbr":
            add_partition_row(fs="fat32", label="UEFITOOLS")
        else:
            add_partition_row(fs="fat32", size="+32M", label="UEFITOOLS")

    def get_partitions() -> list[PartitionSpec]:
        result: list[PartitionSpec] = []
        for _, t, s, l, c, d in partition_rows:
            cs_str = c.get().strip()
            cs = int(cs_str) if cs_str.isdigit() else 0
            result.append(PartitionSpec(t.get(), s.get(),
                                        l.get() or "UEFITOOLS", d.get(), cs))
        return result

    tk.Radiobutton(part_frame, text="None", variable=part_scheme_var,
                   value="none", command=on_scheme_change).pack(side=tk.LEFT)
    tk.Radiobutton(part_frame, text="MBR", variable=part_scheme_var,
                   value="mbr", command=on_scheme_change).pack(
        side=tk.LEFT, padx=10)
    tk.Radiobutton(part_frame, text="GPT", variable=part_scheme_var,
                   value="gpt", command=on_scheme_change).pack(
        side=tk.LEFT, padx=10)
    ToolTip(part_frame, "None=raw filesystem, MBR=legacy boot, GPT=UEFI boot")

    # Partition list editor
    tk.Label(options_tab, text="Partitions:").grid(
        row=1, column=0, sticky=tk.NW, **pad)
    part_btn_frame = tk.Frame(options_tab)
    part_btn_frame.grid(row=1, column=1, columnspan=3, sticky=tk.W, **pad)
    tk.Button(part_btn_frame, text="Add Partition",
              command=add_partition_row).pack(side=tk.LEFT, padx=(0, 5))
    tk.Button(part_btn_frame, text="Remove Last",
              command=remove_partition_row).pack(side=tk.LEFT)

    # Column headers
    header_frame = tk.Frame(options_tab)
    header_frame.grid(row=2, column=0, columnspan=4, sticky=tk.W,
                      padx=15, pady=(2, 0))
    tk.Label(header_frame, text="Type", width=9, anchor=tk.W,
             font=("Segoe UI", 8)).pack(side=tk.LEFT)
    tk.Label(header_frame, text="Size", width=8, anchor=tk.W,
             font=("Segoe UI", 8)).pack(side=tk.LEFT)
    tk.Label(header_frame, text="Label", width=10, anchor=tk.W,
             font=("Segoe UI", 8)).pack(side=tk.LEFT)
    tk.Label(header_frame, text="Cluster", width=7, anchor=tk.W,
             font=("Segoe UI", 8)).pack(side=tk.LEFT)
    tk.Label(header_frame, text="Source Dir", anchor=tk.W,
             font=("Segoe UI", 8)).pack(side=tk.LEFT, fill=tk.X,
                                         expand=True)

    # Scrollable partition row container
    partition_frame = tk.Frame(options_tab)
    partition_frame.grid(row=3, column=0, columnspan=4, sticky=tk.EW,
                         padx=10, pady=2)

    # ISO hybrid
    hybrid_var = tk.BooleanVar(value=False)
    tk.Checkbutton(options_tab, text="Hybrid ISO (dd-writable to USB)",
                   variable=hybrid_var).grid(
        row=4, column=0, columnspan=2, sticky=tk.W, **pad)

    # Build options
    ttk.Separator(options_tab, orient=tk.HORIZONTAL).grid(
        row=5, column=0, columnspan=4, sticky=tk.EW, padx=10, pady=5)

    verify_var = tk.BooleanVar(value=False)
    tk.Checkbutton(options_tab, text="Verify (SHA256 after build)",
                   variable=verify_var).grid(
        row=6, column=0, columnspan=2, sticky=tk.W, **pad)

    verbose_var = tk.BooleanVar(value=False)
    tk.Checkbutton(options_tab, text="Verbose output",
                   variable=verbose_var).grid(
        row=7, column=0, columnspan=2, sticky=tk.W, **pad)

    force_var = tk.BooleanVar(value=False)
    tk.Checkbutton(options_tab, text="Force (skip USB confirmation)",
                   variable=force_var).grid(
        row=8, column=0, columnspan=2, sticky=tk.W, **pad)

    # Populate default partition row (None scheme = one fat32 row)
    on_scheme_change()

    # ===================== LOG TAB =====================
    log_tab = ttk.Frame(notebook)
    notebook.add(log_tab, text="Log")

    log_text = scrolledtext.ScrolledText(log_tab, height=20, width=80,
                                          state=tk.NORMAL, font=("Consolas", 9))
    log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=(5, 0))
    log_text.insert(tk.END, "Ready.\n")

    status_label = tk.Label(log_tab, text="Ready", anchor=tk.W, fg="gray")
    status_label.pack(fill=tk.X, padx=5, pady=(0, 5))

    # ===================== HELP TAB =====================
    help_tab = ttk.Frame(notebook)
    notebook.add(help_tab, text="Help")

    help_text = tk.Text(help_tab, wrap=tk.WORD, font=("Segoe UI", 10),
                        padx=10, pady=10, state=tk.NORMAL)
    help_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    help_text.tag_configure("header", font=("Segoe UI", 11, "bold"),
                            foreground="#2878d0", spacing1=8, spacing3=4)
    help_text.tag_configure("body", font=("Segoe UI", 10),
                            lmargin1=10, lmargin2=10)
    help_text.tag_configure("sep", font=("Segoe UI", 4))

    help_text.insert(tk.END, "Quick Start\n", "header")
    help_text.insert(tk.END,
                     "1. Select a source directory containing your files\n"
                     "2. Choose output format (Image or ISO)\n"
                     "3. Set a target file path or select USB Drive\n"
                     '4. Click "Create Image" or "Write to USB"\n', "body")
    help_text.insert(tk.END, "\n", "sep")

    help_text.insert(tk.END, "Options Reference\n", "header")
    help_text.insert(tk.END,
                     "Partition     None (raw), MBR (legacy BIOS), GPT (UEFI boot)\n"
                     "ISO Hybrid    Makes ISO dd-writable to USB\n"
                     "Verify        SHA256 check after build\n"
                     "Verbose       Show per-file output\n"
                     "Force         Skip USB confirmation prompt\n"
                     "\n"
                     "Partition Spec (CLI: --partition TYPE:SIZE:LABEL[:DIR]):\n"
                     "  TYPE:  esp, fat32, exfat, ntfs\n"
                     "  SIZE:  64M (fixed), +32M (content + extra),\n"
                     "         0 (rest of disk), empty (auto)\n"
                     "  LABEL: volume label (11 chars max)\n"
                     "  DIR:   optional source directory\n", "body")
    help_text.insert(tk.END, "\n", "sep")

    help_text.insert(tk.END, "Tips\n", "header")
    help_text.insert(tk.END,
                     "- ISO files auto-extract to bootable USB (non-hybrid)\n"
                     "- Use .img.gz extension for compressed output\n"
                     "- Works natively on Windows (no WSL needed)\n"
                     "- FAT32 images don't need root; GPT/MBR do\n"
                     "- Check Drive button tests USB for bad blocks\n"
                     "- Persistent checkbox adds ext4 partition for live Linux\n"
                     "- --modify flag (CLI only) edits images without rebuild\n"
                     "- Volume labels are limited to 11 characters for FAT32\n"
                     "- Clone USB drives: use /dev/sdX or 'usb' as source\n", "body")
    help_text.insert(tk.END, "\n", "sep")

    help_text.insert(tk.END, "About\n", "header")
    help_text.insert(tk.END,
                     "mkimage - Bootable Media Creator\n"
                     "Cross-platform tool for creating UEFI boot images,\n"
                     "ISOs, and USB drives.\n"
                     "\n"
                     "https://github.com/mgosha/mkimage\n", "body")

    help_text.config(state=tk.DISABLED)

    root.after(100, poll_log)
    root.mainloop()


if __name__ == "__main__":
    gui_main()
