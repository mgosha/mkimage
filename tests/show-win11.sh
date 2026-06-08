#!/bin/bash
# Boot the Windows 11 test VM under QEMU and stream its desktop to a VNC
# viewer on the user's machine over REVERSE VNC — the mkimage "show it to
# me" path for the native Windows GUI. Modeled on axl-sdk's show-it.sh.
#
# Unlike the axl-sdk case (which boots a UEFI .efi and VNC shows the GOP
# framebuffer), this boots a full Windows 11 guest (the existing
# win11-epsa-build qcow2, already installed and SSH-reachable as `winvm`)
# so mkimage's Python/Dear PyGui GUI can run natively inside it.
#
# Reverse VNC: QEMU connects OUT to a viewer listening on the user's
# machine, so no inbound port/tunnel is needed. The viewer host defaults
# to field 1 of $SSH_CONNECTION (the SSH client = the MacBook running the
# listen loop). Override with SHOW_IT_HOST. Port defaults to 9999
# (SHOW_IT_PORT) to match axl-sdk's show-it.sh.
#
# The viewer side is the user's responsibility — a persistent TigerVNC
# listen loop on the MacBook, e.g.:
#   while true; do vncviewer -listen 9999 -SecurityTypes None; done
#
# Provisioning (sync + auto-launch the GUI) is handled by a background
# helper once SSH comes up; see provision_guest().
#
# Usage: ./tests/show-win11.sh                  # interactive, reverse-VNC
#        ./tests/show-win11.sh --screenshot out.png
#
# --screenshot FILE is a one-shot HEADLESS capture: boot, provision (sync +
# auto-launch the GUI), let it render (SHOT_WAIT secs), screendump the guest
# framebuffer to FILE, then power off — no MacBook viewer involved. The dest
# extension picks the format (.ppm = raw, .png/.jpg via ImageMagick/Pillow).
#
# Env:
#   SHOW_IT_HOST   viewer IP        (default: $SSH_CONNECTION field 1)
#   SHOW_IT_PORT   viewer port      (default: 9999)
#   SHOT_WAIT      secs to wait for the GUI to render before capture (def 8;
#                  raise it if the guest must pip-install dearpygui first)
#   QEMU           qemu binary      (default: qemu-system-x86_64 / qemu-kvm)
#   VM_DIR         VM directory     (default: ~/VMs/win11-epsa-build)
#   VM_MEM         guest RAM MB     (default: 8192)
#   VM_SMP         guest vCPUs      (default: 4)
#   NO_PROVISION=1 boot only; skip code sync + GUI auto-launch
#   GUI=tk         force the Tkinter GUI instead of Dear PyGui
#   GUI=ps1        launch the native PowerShell WinForms GUI (mkimage.ps1)
#                  instead of the Python pyz GUI

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Phase timing: set TIMING=1 to print [+Ns] markers to stderr. SECONDS is a
# bash builtin counting wall-seconds since this assignment.
SECONDS=0
tlog() { [[ -n "${TIMING:-}" ]] && printf '[+%3ds] %s\n' "$SECONDS" "$1" >&2 || true; }

# ---- args ------------------------------------------------------------------
SCREENSHOT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --screenshot) SCREENSHOT="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "show-win11: unknown argument '$1'" >&2; exit 2 ;;
    esac
done
[[ "$SCREENSHOT" == "--screenshot" || ( -n "$SCREENSHOT" && "${SCREENSHOT:0:1}" == "-" ) ]] && {
    echo "show-win11: --screenshot needs a file path (e.g. --screenshot out.png)" >&2; exit 2; }

# ---- viewer endpoint (interactive mode only) -------------------------------
# Screenshot mode is headless: it serves VNC on localhost and never touches
# the user's machine, so a viewer host is irrelevant there.
PORT="${SHOW_IT_PORT:-9999}"
HOST="${SHOW_IT_HOST:-}"
if [[ -z "$HOST" ]]; then
    HOST="$(printf '%s' "${SSH_CONNECTION:-}" | awk '{print $1}')"
fi
if [[ -z "$SCREENSHOT" && -z "$HOST" ]]; then
    echo "show-win11: cannot derive viewer host — set SHOW_IT_HOST=<ip> (no \$SSH_CONNECTION)" >&2
    exit 1
fi

# ---- locate QEMU -----------------------------------------------------------
QEMU="${QEMU:-}"
if [[ -z "$QEMU" ]]; then
    for cand in qemu-system-x86_64 /usr/libexec/qemu-kvm; do
        if command -v "$cand" &>/dev/null || [[ -x "$cand" ]]; then
            QEMU="$cand"; break
        fi
    done
fi
[[ -n "$QEMU" ]] && { command -v "$QEMU" &>/dev/null || [[ -x "$QEMU" ]]; } || {
    echo "show-win11: no qemu binary found (set QEMU=/path/to/qemu-system-x86_64)" >&2
    exit 1
}

# ---- VM assets -------------------------------------------------------------
VM_DIR="${VM_DIR:-$HOME/VMs/win11-epsa-build}"
DISK="$VM_DIR/disk.qcow2"
USB_RAW="$VM_DIR/usb-test.raw"
VARS="$VM_DIR/OVMF_VARS.fd"
OVMF_CODE="/usr/share/edk2/ovmf/OVMF_CODE.fd"
OVMF_VARS_TMPL="/usr/share/edk2/ovmf/OVMF_VARS.fd"
VM_MEM="${VM_MEM:-8192}"
VM_SMP="${VM_SMP:-4}"

[[ -f "$DISK" ]] || { echo "show-win11: disk not found: $DISK" >&2; exit 1; }
[[ -f "$OVMF_CODE" ]] || { echo "show-win11: OVMF_CODE not found: $OVMF_CODE" >&2; exit 1; }

# Per-VM writable EFI vars (seeded once from the system template).
if [[ ! -f "$VARS" ]]; then
    cp "$OVMF_VARS_TMPL" "$VARS"
    echo "show-win11: seeded writable EFI vars at $VARS"
fi

# Safe fake USB target for mkimage write tests (256 MB) if missing.
if [[ ! -f "$USB_RAW" ]]; then
    qemu-img create -f raw "$USB_RAW" 256M >/dev/null
    echo "show-win11: created fake USB target $USB_RAW"
fi

# ---- guest provisioning (background) --------------------------------------
# Waits for the winvm SSH forward to come up, pushes the freshly-built
# mkimage.pyz, then launches the GUI on the *interactive* desktop session
# (so it shows on the VNC console, not invisibly in SSH's session).
provision_guest() {
    local gui="${GUI:-dpg}"
    local pyz="$ROOT_DIR/mkimage.pyz"
    # Windows username on the guest (GUEST_USER overrides; default "user").
    local guser="${GUEST_USER:-user}"
    local guest_dir="C:/Users/$guser/mkimage"

    # The ps1 GUI ships the PowerShell helper as-is — no zipapp build needed.
    if [[ "$gui" != ps1 ]]; then
        # Rebuild the zipapp so the guest always gets current source.
        if [[ -f "$ROOT_DIR/build_pyz.py" ]]; then
            ( cd "$ROOT_DIR" && python3 build_pyz.py ) >/dev/null 2>&1 || true
        fi
        tlog "pyz built"
    fi

    echo "[provision] waiting for winvm SSH (this can take 30-90s while Windows boots)..." >&2
    local tries=0
    until ssh -o ConnectTimeout=4 -o BatchMode=yes winvm "echo ok" 2>/dev/null | grep -q ok; do
        tries=$((tries + 1))
        if (( tries > 90 )); then
            echo "[provision] gave up waiting for winvm SSH; launch the GUI manually in the VNC session." >&2
            return 1
        fi
        sleep 3
    done
    tlog "winvm SSH up"
    echo "[provision] winvm is up — syncing $([[ "$gui" == ps1 ]] && echo mkimage.ps1 || echo mkimage.pyz)" >&2

    ssh -o BatchMode=yes winvm "powershell -NoProfile -Command \"New-Item -ItemType Directory -Force -Path '$guest_dir' | Out-Null\"" 2>/dev/null || true
    if [[ "$gui" == ps1 ]]; then
        scp -o BatchMode=yes "$ROOT_DIR/mkimage.ps1" "winvm:$guest_dir/mkimage.ps1" >/dev/null 2>&1 || {
            echo "[provision] scp of mkimage.ps1 failed" >&2; return 1; }
    else
        scp -o BatchMode=yes "$pyz" "winvm:$guest_dir/mkimage.pyz" >/dev/null 2>&1 || {
            echo "[provision] scp of mkimage.pyz failed" >&2; return 1; }
    fi

    # Keep the guest display awake so a slow capture never lands on a blanked
    # (black) screen. Idempotent; cheap. (AC + DC, monitor + standby.)
    ssh -o BatchMode=yes winvm "powercfg /change monitor-timeout-ac 0 & powercfg /change monitor-timeout-dc 0 & powercfg /change standby-timeout-ac 0 & powercfg /change standby-timeout-dc 0" >/dev/null 2>&1 || true

    # A GUI can only appear on the VNC console once the user is logged into
    # the interactive session. The VM is configured for autologon, so this is
    # satisfied a few seconds after boot. Detect it by the desktop shell
    # (explorer.exe) being present — far more robust than parsing `query
    # session` text over SSH. Fail FAST (not after 10 min) if it never logs in.
    echo "[provision] waiting for the interactive desktop session (autologon)..." >&2
    local stries=0 nexpl
    until nexpl="$(ssh -o BatchMode=yes winvm 'powershell -NoProfile -Command "(Get-Process explorer -ErrorAction SilentlyContinue | Measure-Object).Count"' 2>/dev/null | tr -dc '0-9')"; [[ "${nexpl:-0}" -ge 1 ]]; do
        stries=$((stries + 1))
        if (( stries > 30 )); then   # ~90s — autologon should be well under this
            echo "[provision] no interactive desktop after 90s (autologon may have failed); log in via VNC, then run 'python $guest_dir/mkimage.pyz'." >&2
            return 1
        fi
        sleep 3
    done
    tlog "interactive session up (autologon)"

    # Launch on the interactive desktop via a scheduled task whose principal is
    # the logged-on user with an Interactive logon type, so the window draws on
    # the console (session 1 / VNC) rather than SSH's non-interactive session 0.
    #
    # The launch command is staged as a run-gui.cmd ON THE GUEST instead of
    # passed inline: cmd.exe does NOT treat single quotes as quoting, so inline
    # quoting through ssh -> cmd -> powershell -> cmd is a minefield. A .cmd
    # file sidesteps all of it and makes the dpg/tk choice a content swap.
    local win_pyz="C:\\Users\\$guser\\mkimage\\mkimage.pyz"
    local win_ps1="C:\\Users\\$guser\\mkimage\\mkimage.ps1"
    local win_cmd="C:\\Users\\$guser\\mkimage\\run-gui.cmd"
    local tmpcmd; tmpcmd="$(mktemp "${TMPDIR:-/tmp}/mkimage-gui.XXXXXX")"
    if [[ "$gui" == ps1 ]]; then
        # No args = WinForms GUI. Bypass execution policy for the unsigned script.
        printf '@echo off\r\npowershell -NoProfile -ExecutionPolicy Bypass -File "%s"\r\n' "$win_ps1" > "$tmpcmd"
    elif [[ "$gui" == tk ]]; then
        printf '@echo off\r\npython -c "import sys; sys.path.insert(0, r'\''%s'\''); from mkimage.gui_tk import gui_main; gui_main()"\r\n' "$win_pyz" > "$tmpcmd"
    else
        printf '@echo off\r\npython "%s"\r\n' "$win_pyz" > "$tmpcmd"
    fi
    scp -o BatchMode=yes "$tmpcmd" "winvm:$guest_dir/run-gui.cmd" >/dev/null 2>&1 || true
    rm -f "$tmpcmd"

    echo "[provision] interactive session up — launching $gui GUI on the desktop..." >&2
    ssh -o BatchMode=yes winvm "powershell -NoProfile -ExecutionPolicy Bypass -Command \"\$a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c $win_cmd'; \$p = New-ScheduledTaskPrincipal -UserId \$env:USERNAME -LogonType Interactive -RunLevel Highest; Register-ScheduledTask -TaskName mkimage-gui -Action \$a -Principal \$p -Force | Out-Null; Start-ScheduledTask -TaskName mkimage-gui\"" 2>/dev/null \
        || echo "[provision] auto-launch failed — run 'python $win_pyz' in the VNC session." >&2
    tlog "GUI launched"
}

# ---- launch ----------------------------------------------------------------
# Distro qemu-kvm is the proven Windows path (see git/bash history). q35 +
# KVM + host CPU, virtio disk/net, OVMF pflash with writable vars, usb-tablet
# for an absolute VNC pointer, the fake USB drive as removable storage, and a
# user-net with SSH (2222) + RDP (3389) forwarded to the guest. The display
# differs per mode (set below).
CMD=(
    "$QEMU"
    -name "mkimage-win11"
    -machine type=q35,accel=kvm
    -cpu host -smp "$VM_SMP" -m "$VM_MEM"
    -drive if=pflash,format=raw,readonly=on,file="$OVMF_CODE"
    -drive if=pflash,format=raw,file="$VARS"
    -drive file="$DISK",format=qcow2,if=virtio
    -drive file="$USB_RAW",format=raw,if=none,id=usbdisk0
    -device qemu-xhci,id=xhci
    -device usb-tablet,bus=xhci.0
    -device usb-storage,bus=xhci.0,drive=usbdisk0,removable=on
    -vga virtio
    -nic user,model=virtio-net-pci,hostfwd=tcp::2222-:22,hostfwd=tcp::3389-:3389
)

if [[ -z "$SCREENSHOT" ]]; then
    # ---- interactive mode: stream to the user's VNC viewer, block on QEMU ---
    # A monitor socket lets us shut the guest down GRACEFULLY (ACPI
    # system_powerdown) on Ctrl-C instead of killing QEMU — a hard kill is a
    # power-yank that can flag Windows' dirty-shutdown bit and drop the next
    # boot into recovery (WinRE). Never SIGKILL this VM.
    PROV_PID=""
    MONSOCK="$(mktemp -u "${TMPDIR:-/tmp}/mkimage-mon.XXXXXX.sock")"
    # QMP socket (machine protocol) for scripted input injection / automation
    # — e.g. driving the GUI for screenshot tests. HMP (MONSOCK) is for humans.
    QMPSOCK="$(mktemp -u "${TMPDIR:-/tmp}/mkimage-qmp.XXXXXX.sock")"
    QEMU_PID=""
    shutdown_guest() {
        if [[ -n "$QEMU_PID" ]] && kill -0 "$QEMU_PID" 2>/dev/null; then
            echo "show-win11: sending ACPI shutdown to the guest (clean power-off)..." >&2
            echo "system_powerdown" | socat -t 2 - "UNIX-CONNECT:$MONSOCK" >/dev/null 2>&1 || true
            for _ in $(seq 1 60); do kill -0 "$QEMU_PID" 2>/dev/null || break; sleep 1; done
            kill -0 "$QEMU_PID" 2>/dev/null && { echo "show-win11: guest didn't power off in 60s — terminating QEMU." >&2; kill "$QEMU_PID" 2>/dev/null || true; }
        fi
        [[ -n "$PROV_PID" ]] && kill "$PROV_PID" 2>/dev/null || true
        rm -f "$MONSOCK" 2>/dev/null || true
    }
    trap 'shutdown_guest; exit 0' INT TERM

    echo "show-win11: reverse VNC -> $HOST:$PORT  (your TigerVNC must be listening there)"
    echo "show-win11: QEMU=$QEMU  disk=$DISK"
    echo "show-win11: winvm SSH on localhost:2222; Ctrl-C here for a CLEAN guest shutdown."

    if [[ "${NO_PROVISION:-}" != "1" ]]; then
        provision_guest &
        PROV_PID=$!
    fi
    "${CMD[@]}" -display "vnc=$HOST:$PORT,reverse=on" \
                -monitor "unix:$MONSOCK,server,nowait" \
                -qmp "unix:$QMPSOCK,server,nowait" &
    QEMU_PID=$!
    wait "$QEMU_PID"
    [[ -n "$PROV_PID" ]] && kill "$PROV_PID" 2>/dev/null || true
    rm -f "$MONSOCK" "$QMPSOCK" 2>/dev/null || true
    exit 0
fi

# ---- screenshot mode: headless boot, capture, power off --------------------
# Serve VNC only on localhost (nobody connects) and add a monitor socket for
# screendump. Display number derived from PID to avoid collisions.
VNC_N=$(( $$ % 80 + 10 ))
MONSOCK="$(mktemp -u "${TMPDIR:-/tmp}/mkimage-mon.XXXXXX.sock")"
SHOT_PPM="$(mktemp "${TMPDIR:-/tmp}/mkimage-shot.XXXXXX.ppm")"

QEMU_PID=""
cleanup_shot() {
    [[ -n "$QEMU_PID" ]] && kill "$QEMU_PID" 2>/dev/null
    rm -f "$MONSOCK" "$SHOT_PPM" 2>/dev/null || true
}
trap cleanup_shot EXIT INT TERM

echo "show-win11: screenshot mode — headless capture to $SCREENSHOT" >&2
echo "show-win11: QEMU=$QEMU  disk=$DISK  (local VNC :$VNC_N, no viewer)" >&2

"${CMD[@]}" -display "vnc=127.0.0.1:$VNC_N" \
            -monitor "unix:$MONSOCK,server,nowait" >/dev/null 2>&1 &
QEMU_PID=$!
tlog "qemu spawned"

# Provision synchronously (waits for SSH + autologon, then launches the GUI),
# unless boot-only was requested.
if [[ "${NO_PROVISION:-}" != "1" ]]; then
    provision_guest || echo "[provision] continuing to capture whatever is on screen" >&2
fi

# Let the GUI finish rendering, then dump (retry — the monitor may lag).
sleep "${SHOT_WAIT:-8}"
tlog "render wait done (SHOT_WAIT=${SHOT_WAIT:-8})"
for try in 1 2 3; do
    echo "screendump $SHOT_PPM" | socat -t 2 - "UNIX-CONNECT:$MONSOCK" >/dev/null 2>&1 && break
    sleep 1
done
sleep 1
tlog "screendump captured"
kill "$QEMU_PID" 2>/dev/null || true; wait "$QEMU_PID" 2>/dev/null || true
QEMU_PID=""

[[ -s "$SHOT_PPM" ]] || { echo "show-win11: screenshot capture failed (empty dump)" >&2; exit 1; }

# QEMU writes PPM; convert to the destination's extension (PPM = copy).
ext="${SCREENSHOT##*.}"; ext="${ext,,}"
if [[ "$ext" == "ppm" ]]; then
    cp "$SHOT_PPM" "$SCREENSHOT"
elif command -v convert &>/dev/null; then
    convert "$SHOT_PPM" "$SCREENSHOT"
elif python3 -c 'import PIL' 2>/dev/null; then
    python3 -c 'import sys; from PIL import Image; Image.open(sys.argv[1]).save(sys.argv[2])' "$SHOT_PPM" "$SCREENSHOT"
else
    echo "show-win11: need ImageMagick (convert) or Python Pillow for .$ext output — use a .ppm dest to skip conversion" >&2
    cp "$SHOT_PPM" "${SCREENSHOT%.*}.ppm"
    echo "show-win11: raw PPM saved instead: ${SCREENSHOT%.*}.ppm" >&2
    exit 1
fi
tlog "converted + saved"
echo "show-win11: screenshot saved: $SCREENSHOT"
