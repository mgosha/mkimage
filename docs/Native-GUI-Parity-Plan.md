# Native (PowerShell) GUI Parity Plan

Status: **Planning** · Owner: mkimage maintainers · Target: `mkimage.ps1`
`Show-MainForm`

## Goal

Bring the native Windows WinForms GUI (`mkimage.ps1`) to full feature
parity with the Dear PyGui GUI (`mkimage/gui_dpg.py`), reusing the Python
GUI's **layout** (tab structure, panel grouping, control order) while
rendering it with **native Windows controls** for a native look and feel.

Two outcomes:

1. **Same layout, native chrome.** The Python GUI's information
   architecture is good; its *look* is not (to the maintainer). Mirror the
   tabbed layout exactly in WinForms using stock Windows controls and
   system theming, plus the existing accent header banner as the single
   flourish.
2. **Native GUI becomes the Windows default.** When running on Windows and
   `powershell` is available, the native WinForms GUI launches by default
   (no args / `--gui`). The Dear PyGui interface remains available via an
   explicit override and stays the default on Linux/macOS.

This is a **deliberate deviation** from `docs/Design.md`, which currently
names Dear PyGui the primary GUI on all platforms. Design.md and CLAUDE.md
must be updated as part of this work (Phase 7). Per project rules, flag and
validate this deviation before flipping the default.

## Design principles

- **Layout is the contract.** Tabs, panel grouping, control order, and
  labels match `gui_dpg.py` so muscle memory and docs transfer between the
  two. Where a Python feature has no native equivalent, the control still
  appears but is disabled with a tooltip explaining the limitation (same
  pattern the Python GUI uses to grey out unavailable filesystems).
- **Native first.** Use stock `System.Windows.Forms` controls
  (`TabControl`, `GroupBox`, `DataGridView` for partition rows, native
  file/folder dialogs, `ProgressBar`). Keep the gradient header; otherwise
  defer to system colors and fonts. No attempt to imitate the Dear PyGui
  dark theme.
- **GUI ≠ backend.** Many Python features are not just unwired in the
  WinForms GUI — the PowerShell backend cannot do them yet. Each phase
  separates **GUI work** from **backend work** and calls out Windows-native
  limitations explicitly. No feature is "exposed" in the GUI until the
  backend behind it actually works (no dead buttons shipped).
- **Verifiable.** Every phase ends with a QEMU + OVMF screenshot/boot check
  via `tests/show-win11.sh GUI=ps1` and, where the operation produces
  media, a boot-verify of the output.

## Current state (baseline)

**WinForms GUI today** (`Show-MainForm`): single panel. Source directory +
Browse; Extra Includes (Add File/Dir/Clear + listbox); Format Image/ISO;
Volume Label; Filesystem combo (FAT32/NTFS/exFAT); Extra Space (MB);
Verbose/Verify/GPT/Write-to-USB checkboxes; USB drive list + Refresh; Log
pane; restyled with accent header + flat buttons + dark log.

**PowerShell backend today:**

| Function | Capability |
|---|---|
| `Get-UsbDrives` | Enumerate removable drives |
| `New-UefiImage` / `New-Fat32Image` | FAT32 `.img` via VHD + diskpart. No GPT/MBR, no fs choice, no multi-partition. `SizeMB` = extra space |
| `New-IsoImage` | ISO with El Torito EFI (oscdimg / IMAPI2). `BootImage` param |
| `Write-UsbDrive` | Format USB (FAT32/NTFS/exFAT) + copy. `UseGpt`, `Verify` |
| `Show-MainForm` | The GUI |

`-Action` verbs: `WriteUsb`, `CreateImg`, `CreateIso`.

## Parity matrix

Legend — **GUI**: needs WinForms wiring. **BE**: needs new/changed
PowerShell backend. **Limit**: Windows-native constraint to resolve.

| Feature (from `gui_dpg.py`) | GUI | BE | Phase | Notes / native limitation |
|---|:--:|:--:|:--:|---|
| Tabbed UI (Build/Options/Tools/Log/Help) | ✅ | — | 1 | `TabControl` |
| Accent/native look, system theme | ✅ | — | 1 | Keep header banner only |
| F-key navigation (F1–F9, F12) | ✅ | — | 1 | `KeyPreview` + handlers |
| Progress bar + status line | ✅ | ◑ | 1 | Backend already writes a progress file; surface it |
| Source = dir / `.img` / `.iso` / device | ✅ | ◑ | 2 | Detect type; image/iso/device sources |
| USB as source → Clone | ✅ | ✅ | 2 | New clone backend |
| Clone to image / USB / USB→USB | ✅ | ✅ | 2 | `Clone` action(s) |
| Dynamic action label (Create/Write/Clone) | ✅ | — | 2 | |
| Partition scheme None / MBR / GPT (image) | ✅ | ✅ | 3 | diskpart GPT/MBR in VHD |
| Multi-partition editor | ✅ | ✅ | 3 | `DataGridView`; diskpart loop |
| Per-partition fs / size / label / source | ✅ | ✅ | 3 | |
| Cluster size | ✅ | ✅ | 3 | diskpart `format ... unit=` |
| Hybrid ISO | ✅ | ◑ | 4 | oscdimg already makes El Torito; confirm dd-writable |
| UDF bridge (>4 GB) | ✅ | ✅ | 4 | oscdimg `-u2`/`-udfver` |
| Output compression `.img.gz` | ✅ | ✅ | 4 | `GzipStream` |
| Tools: Format drive (standalone) | ✅ | ◑ | 5 | Reuse `Write-UsbDrive` format path |
| Tools: Wipe drive | ✅ | ◑ | 5 | diskpart `clean` |
| Tools: Check drive / bad blocks | ✅ | ✅ | 5 | **Limit**: no `badblocks`; write/verify pattern pass in PS |
| Tools: List image contents | ✅ | ✅ | 5 | **Limit**: FAT + partition-table parser in PS |
| Persistent partition (live Linux, ext4) | ◑ | ✅ | 5 | **Limit**: Windows can't `mkfs.ext4` natively — see Risks |
| ext4 / udf filesystem choices | ◑ | ✅ | 3/5 | **Limit**: ext4 not native; udf via `format /fs:udf` |
| Filesystem availability greying | ✅ | ✅ | 6 | Probe what diskpart/format supports |
| Help tab (shortcuts, tips, about, link) | ✅ | — | 6 | Static text |
| Force (skip USB confirm) | ✅ | ◑ | 2 | `SkipConfirm` already exists |
| Native default on Windows | ✅ | — | 7 | cli.py flip + docs |

`◑` = partially exists / mostly wiring.

## Layout mapping (Python tab → WinForms tab)

```
Dear PyGui                         WinForms (TabControl)
─────────────────────────────────  ─────────────────────────────────
Build  (F1)                        Build  (F1)
  Source panel (File/USB radio)      GroupBox "Source"
  Additional includes                GroupBox "Includes"
  Target panel (File/USB radio)      GroupBox "Target"
  Format img/iso, label, extra       (in Target group)
  Action button + Exit               Action button (dynamic label)
Options (F2)                        Options (F2)
  Partition scheme None/MBR/GPT       GroupBox "Partition Scheme"
  Partition rows editor               GroupBox "Partitions" (DataGridView)
  Hybrid ISO / UDF bridge             GroupBox "ISO"
  Verify / Verbose / Force            GroupBox "Build Options"
Tools  (F3)                        Tools  (F3)
  Format / Wipe / Check / List        4 GroupBoxes
Log    (F4)                        Log    (F4)  — existing dark log pane
Help   (F5)                        Help   (F5)  — static help text
```

Status bar (progress bar + status label) sits below the `TabControl` on all
tabs, mirroring the Python GUI's always-visible footer.

## Phases

Each phase is independently shippable, leaves the GUI fully working, and is
boot/screenshot-verified before the next begins.

### Phase 1 — Tabbed shell, native look, navigation, status
- Refactor `Show-MainForm` into a `TabControl` with the five tabs above;
  move all existing controls into the **Build** tab unchanged in behavior.
- Footer: `ProgressBar` (marquee while running, determinate from the
  existing progress file when available) + status `Label`.
- `KeyPreview` handlers for F1–F5 (tabs), F6 (refresh USB), F7/F8/F9
  (Tools actions, wired in Phase 5 — disabled until then), F12 (action).
- Add cli override plumbing only: `--python-gui` flag and the
  Windows+PowerShell detection helper (default flip stays OFF until Phase 7
  to avoid regressing Windows users to a not-yet-at-parity GUI).
- **GUI only.** Verify: screenshot each tab renders; existing FAT32 `.img`
  + ISO + USB build paths still work.

### Phase 2 — Source/target flexibility + clone
- Build tab: Source mode File/USB; accept dir, `.img`, `.iso`, `/dev`-style
  PhysicalDrive as source; Target mode File/USB; dynamic action label
  (Create Image / Write to USB / Clone to Image / Clone to USB); Force
  checkbox (maps to existing `SkipConfirm`).
- **Backend:** `Copy-DiskImage` (raw image ↔ PhysicalDrive, PhysicalDrive ↔
  PhysicalDrive) + a `Clone` action verb. Reuse existing safety checks.
- Verify: clone fake USB → image and back in QEMU.

### Phase 3 — Partitioning (Options tab)
- Options tab: scheme None/MBR/GPT; partition `DataGridView`
  (Add/Remove rows) with columns fs / size / label / cluster / source.
- **Backend:** generalize image creation into `New-DiskImage` that drives
  diskpart to build MBR or GPT VHDs with N partitions, per-partition
  filesystem (FAT32/NTFS/exFAT/UDF), cluster size, and label, then copies
  each partition's source. Keep `New-UefiImage` as a thin wrapper for the
  simple single-FAT32 path.
- Verify: GPT ESP+data image boots to UEFI shell in QEMU; partition
  structure matches spec.

### Phase 4 — ISO options + compression
- Options tab: Hybrid ISO + UDF Bridge toggles; ensure Verify applies.
- **Backend:** UDF bridge via oscdimg UDF flags; confirm/produce
  dd-writable hybrid ISO; `Compress-Output` (GzipStream) for `.img.gz`
  targets.
- Verify: `.img.gz` round-trips (decompress matches); UDF ISO mounts and
  holds a >4 GB file; hybrid ISO boots from USB.

### Phase 5 — Tools tab
- Tools tab GroupBoxes: **Format** (drive + scheme + fs + label),
  **Wipe** (diskpart clean), **Check** (bad-blocks-style write/verify pass
  implemented in PowerShell, with a clear "destructive" confirm), **List
  Image Contents** (PowerShell FAT + partition-table reader → Log tab).
- **Persistent partition:** see Risks — likely surfaced as a disabled
  control with an explanatory tooltip unless an ext4 path is chosen.
- Verify: format/wipe a fake USB; check runs and reports; list parses a
  known image.

### Phase 6 — Help tab + parity polish
- Help tab: shortcuts, quick start, options reference, tips, about + GitHub
  link (port the Python GUI's text).
- Filesystem availability detection: grey out fs types the host can't
  actually create (e.g. ext4), matching `get_available_filesystems()`
  behavior.

### Phase 7 — Flip the Windows default + docs + tests
- `cli.py`: on Windows with `powershell` present, default GUI (no args /
  `--gui`) launches the native WinForms GUI; `--python-gui` forces Dear
  PyGui; Linux/macOS unchanged. Keep `--native-gui` as an explicit alias.
- Update `docs/Design.md` (GUI default per platform), `CLAUDE.md`
  (GUI constraints), `README`, and the HANDOFF/memory notes.
- Tests: extend `tests/test_windows.py` with screendump-based assertions
  per tab and per new operation; document `GUI=ps1` flow.

## Risks & native limitations

- **ext4 / persistent partitions.** Windows has no native `mkfs.ext4`. A
  live-Linux persistent `casper-rw` is ext4. Options: (a) ship a bundled
  static `mke2fs` (licensing + size cost — likely no), (b) raw-write a
  pre-zeroed ext4 skeleton, or (c) **degrade gracefully**: show the control
  disabled on Windows with a tooltip. Recommend (c) for Phase 5; revisit if
  there's demand. Decision needed from maintainer.
- **Bad-blocks check.** No native `badblocks`. Implement a destructive
  write-pattern-then-read-verify pass in PowerShell over the raw device.
  Slower and simpler than `badblocks` but adequate; gate behind the same
  destructive confirm.
- **List image contents.** Requires a FAT/partition-table parser in
  PowerShell (the Python side reuses `modify.py`'s FAT reader). Scope to
  FAT32 + GPT/MBR table first; note exFAT/NTFS listing as a follow-up.
- **Default flip timing.** Flipping the Windows default *before* parity
  (Phases 2–6) would regress Windows users to a less-capable GUI.
  Recommendation: land the override flag early (Phase 1) but flip the
  default only in Phase 7. The maintainer asked for native-default; this
  sequencing delivers it without a regression window. Confirm acceptable.
- **`Show-MainForm` size.** Already ~390 lines; full parity will roughly
  double it. Consider extracting per-tab builder functions
  (`New-BuildTab`, `New-OptionsTab`, …) to keep it maintainable, while
  preserving `mkimage.ps1`'s single-file, dependency-free property.

## Out of scope

- `--modify` (image add/remove files) — CLI-only in both GUIs today; not a
  parity gap.
- Imitating the Dear PyGui dark theme — explicitly *not* a goal; native
  look is the goal.

## Verification harness

- `GUEST_USER=mike GUI=ps1 ./tests/show-win11.sh` — interactive
  reverse-VNC, native GUI on the desktop.
- `GUEST_USER=mike GUI=ps1 ./tests/show-win11.sh --screenshot out.png` —
  headless per-phase screenshot.
- Boot-verify produced media with throwaway QEMU + OVMF (see HANDOFF).
