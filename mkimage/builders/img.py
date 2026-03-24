"""FAT32 image builder — no partition table."""
from __future__ import annotations

import os
import subprocess as _sp
import tempfile
from pathlib import Path, PurePosixPath

from mkimage.platform import _is_windows, _run, _which, _resolve
from mkimage.tools import ensure_tools, _suggest_install
from mkimage.verify import _verify_write


_extracted_ps1: str | None = None


def _find_ps1() -> str | None:
    """Locate mkimage.ps1 — filesystem, zipapp resource, or cwd."""
    global _extracted_ps1
    if _extracted_ps1 and os.path.isfile(_extracted_ps1):
        return _extracted_ps1

    # Check filesystem locations
    candidates = [
        Path(__file__).resolve().parent.parent / "mkimage.ps1",
        Path(__file__).resolve().parent.parent.parent / "mkimage.ps1",
        Path.cwd() / "mkimage.ps1",
    ]
    for p in candidates:
        if p.is_file():
            return str(p)

    # Check if bundled inside zipapp — extract to shared temp location
    # so both normal and elevated processes can access it
    try:
        import importlib.resources as pkg_resources
        ref = pkg_resources.files("mkimage").joinpath("mkimage.ps1")
        data = ref.read_bytes()
        shared_dir = os.path.join(os.environ.get("SystemDrive", "C:"), "temp")
        os.makedirs(shared_dir, exist_ok=True)
        tmp_path = os.path.join(shared_dir, "mkimage.ps1")
        with open(tmp_path, "wb") as f:
            f.write(data)
        _extracted_ps1 = tmp_path
        return tmp_path
    except (ImportError, FileNotFoundError, TypeError, AttributeError):
        pass

    return None


def _stage_files(files: dict[str, str], staging: Path) -> None:
    """Stage files into a temp directory preserving structure."""
    import shutil
    for img_path, local_path in files.items():
        dest = staging / img_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)


def _calculate_content_size(files: dict[str, str]) -> int:
    """Return total content size in MB (ceiling)."""
    total_bytes = sum(os.path.getsize(lp) for lp in files.values())
    return max(1, -(-total_bytes // (1024 * 1024)))


def _interpret_size(size_str: str, content_mb: int) -> int:
    """Interpret a size string into total MB.

    '+32M' means content + 32MB extra.  '100M' means exactly 100MB.
    Empty means content + 32MB.  '0' means content + 32MB.
    Minimum is 40MB (FAT32 requirement).
    """
    from mkimage import _parse_size
    if not size_str or size_str == "0":
        return max(40, content_mb + 32)
    if size_str.startswith("+"):
        extra = _parse_size(size_str[1:]) // (1024 * 1024)
        return max(40, content_mb + extra)
    absolute = _parse_size(size_str) // (1024 * 1024)
    return max(40, absolute)


def _populate_img_mcopy(cfg: object, files: dict[str, str], out: str) -> None:
    """Populate a FAT32 image using mcopy (no root needed)."""
    dirs_created: set[str] = set()
    for img_path in sorted(files.keys()):
        parts = PurePosixPath(img_path).parts
        for i in range(1, len(parts)):
            d = str(PurePosixPath(*parts[:i]))
            if d not in dirs_created:
                _run(cfg, ["mmd", "-i", out, f"::{d}"], check=False)
                dirs_created.add(d)
    for img_path, local_path in sorted(files.items()):
        src = _resolve(local_path)
        _run(cfg, ["mcopy", "-i", out, src, f"::{img_path}"],
             verbose=cfg.verbose)
    cfg.log(f"  Copied {len(files)} files via mcopy")


def _populate_img_mount(
    cfg: object, stg_resolved: str, out: str, size_mb: int, label: str,
) -> bool:
    """Populate a FAT32 image via mount+rsync (requires root)."""
    if cfg.verbose:
        rsync_flags = "-av --no-owner --no-group"
    else:
        rsync_flags = "-a --no-owner --no-group --info=progress2"

    dd_cmd = ["dd", "if=/dev/zero", f"of={out}", "bs=1M", f"count={size_mb}"]
    if cfg.verbose:
        dd_cmd.append("status=progress")
    _run(cfg, dd_cmd, verbose=True)
    _run(cfg, ["mkfs.vfat", "-F", "32", "-n", label, out], verbose=True)

    r = _run(cfg, [
        "bash", "-c",
        f"MNTDIR=$(mktemp -d) && "
        f"mount -o loop '{out}' $MNTDIR && "
        f"rsync {rsync_flags} '{stg_resolved}'/ $MNTDIR/ && "
        f"TOTAL=$(find $MNTDIR -type f | wc -l) && "
        f"echo \"Copied $TOTAL files\" && "
        f"umount $MNTDIR && "
        f"rmdir $MNTDIR"
    ], check=False, verbose=True, as_root=True)
    return r.returncode == 0


def _write_fat32_image(
    cfg: object, files: dict[str, str], output: str,
    size_mb: int, label: str, cluster_size: int = 0,
    mbr: bool = False,
) -> None:
    """Create a FAT32 image file using pure Python — no admin, no external tools.

    Writes the FAT32 BPB, FAT tables, root directory, and file data directly.
    Based on the approach Rufus uses (direct filesystem creation).
    Works on all platforms without elevation or external dependencies.

    If mbr=True, prepends an MBR partition table with a single FAT32 LBA
    partition spanning the entire disk. The FAT32 filesystem starts at
    sector 1 (hidden_sectors=1 in BPB).
    """
    import struct

    sector_size = 512
    # MBR: partition starts at sector 2048 (1MB alignment, Windows standard)
    partition_offset = 2048 if mbr else 0
    total_image_sectors = size_mb * 1024 * 1024 // sector_size
    total_sectors = total_image_sectors - partition_offset  # FAT32 portion

    # Choose cluster size (sectors per cluster)
    if cluster_size > 0:
        spc = cluster_size // sector_size
    elif size_mb <= 64:
        spc = 1   # 512 bytes
    elif size_mb <= 128:
        spc = 2   # 1KB
    elif size_mb <= 256:
        spc = 4   # 2KB
    elif size_mb <= 8192:
        spc = 8   # 4KB
    elif size_mb <= 16384:
        spc = 16  # 8KB
    else:
        spc = 32  # 16KB

    reserved_sectors = 32
    num_fats = 2

    def _calc_fat_size(tot: int, res: int, nfat: int, secpc: int) -> int:
        """Calculate FAT size per Microsoft FAT spec (fatgen103.pdf, p.21).

        RootDirSectors = 0 for FAT32.
        TmpVal1 = DskSize - (ResvdSecCnt + RootDirSectors)
        TmpVal2 = (256 * SecPerClus) + NumFATs
        If FAT32: TmpVal2 = TmpVal2 / 2
        FATSz = (TmpVal1 + (TmpVal2 - 1)) / TmpVal2
        """
        t1 = tot - res
        t2 = (256 * secpc + nfat) // 2
        return (t1 + t2 - 1) // t2

    fat_sectors = _calc_fat_size(total_sectors, reserved_sectors, num_fats, spc)
    data_start = reserved_sectors + num_fats * fat_sectors
    data_sectors = total_sectors - data_start
    cluster_count = data_sectors // spc

    # FAT32 requires >= 65525 clusters; if too few, reduce spc
    while cluster_count < 65525 and spc > 1:
        spc //= 2
        fat_sectors = _calc_fat_size(total_sectors, reserved_sectors, num_fats, spc)
        data_start = reserved_sectors + num_fats * fat_sectors
        data_sectors = total_sectors - data_start
        cluster_count = data_sectors // spc

    # Volume serial number (random)
    import random
    vol_serial = random.randint(0, 0xFFFFFFFF)

    # Pad label to 11 chars
    vol_label_bytes = label.upper().ljust(11)[:11].encode("ascii", errors="replace")

    mbr_label = " (MBR)" if mbr else ""
    cfg.log(f"  Creating FAT32{mbr_label} image ({size_mb}MB, {spc * sector_size}B clusters)...")

    with open(output, "wb") as f:
        # === MBR (sector 0) — only if mbr=True ===
        if mbr:
            mbr_sector = bytearray(sector_size)
            # Partition entry 1 at offset 446 (16 bytes)
            mbr_sector[446] = 0x80                                  # Active/bootable
            # CHS start: use LBA-to-CHS for partition_offset (usually 2048)
            # For small disks: H=32, S=32; LBA 2048 = C=2, H=0, S=1
            mbr_sector[447] = 32                                    # Start head
            mbr_sector[448] = 33                                    # Start sector (1-based)
            mbr_sector[449] = 0                                     # Start cylinder
            mbr_sector[450] = 0x0C                                  # Type: FAT32 LBA
            # CHS end: use max values (LBA is authoritative)
            mbr_sector[451] = 0xFE                                  # End head
            mbr_sector[452] = 0xFF                                  # End sector+cyl
            mbr_sector[453] = 0xFF                                  # End cylinder
            struct.pack_into('<I', mbr_sector, 454, partition_offset)  # LBA start
            struct.pack_into('<I', mbr_sector, 458, total_sectors)    # LBA size
            mbr_sector[510] = 0x55                                  # Boot signature
            mbr_sector[511] = 0xAA
            f.write(mbr_sector)
            # Gap between MBR and partition (sectors 1 to partition_offset-1)
            f.write(b'\x00' * sector_size * (partition_offset - 1))

        # === Boot sector (sector 0 of FAT32 partition) ===
        boot = bytearray(sector_size)
        boot[0:3] = b'\xEB\x58\x90'          # Jump + NOP
        boot[3:11] = b'MKIMAGE '              # OEM name
        struct.pack_into('<H', boot, 11, sector_size)      # Bytes per sector
        boot[13] = spc                                      # Sectors per cluster
        struct.pack_into('<H', boot, 14, reserved_sectors)  # Reserved sectors
        boot[16] = num_fats                                 # Number of FATs
        struct.pack_into('<H', boot, 17, 0)                 # Root entries (0 for FAT32)
        struct.pack_into('<H', boot, 19, 0)                 # Total sectors 16 (0 for FAT32)
        boot[21] = 0xF8                                     # Media descriptor (fixed disk)
        struct.pack_into('<H', boot, 22, 0)                 # FAT size 16 (0 for FAT32)
        struct.pack_into('<H', boot, 24, 32)                # Sectors per track
        struct.pack_into('<H', boot, 26, 8)                 # Number of heads
        struct.pack_into('<I', boot, 28, partition_offset)    # Hidden sectors
        struct.pack_into('<I', boot, 32, total_sectors)     # Total sectors 32
        # FAT32-specific fields (offset 36+)
        struct.pack_into('<I', boot, 36, fat_sectors)       # FAT size 32
        struct.pack_into('<H', boot, 40, 0)                 # Flags
        struct.pack_into('<H', boot, 42, 0)                 # Version
        struct.pack_into('<I', boot, 44, 2)                 # Root cluster
        struct.pack_into('<H', boot, 48, 1)                 # FSInfo sector
        struct.pack_into('<H', boot, 50, 6)                 # Backup boot sector
        boot[64] = 0x80                                     # Drive number
        boot[66] = 0x29                                     # Extended boot signature
        struct.pack_into('<I', boot, 67, vol_serial)        # Volume serial
        boot[71:82] = vol_label_bytes                       # Volume label
        boot[82:90] = b'FAT32   '                          # FS type
        boot[510] = 0x55                                    # Boot signature
        boot[511] = 0xAA
        f.write(boot)

        # === FSInfo sector (sector 1) — placeholder, updated after allocation ===
        fsinfo = bytearray(sector_size)
        struct.pack_into('<I', fsinfo, 0, 0x41615252)      # Lead signature
        struct.pack_into('<I', fsinfo, 484, 0x61417272)     # Struct signature
        struct.pack_into('<I', fsinfo, 488, 0xFFFFFFFF)     # Free clusters (placeholder)
        struct.pack_into('<I', fsinfo, 492, 0xFFFFFFFF)     # Next free cluster (placeholder)
        fsinfo[510] = 0x55
        fsinfo[511] = 0xAA
        f.write(fsinfo)

        # Sectors 2-5: zeros
        f.write(b'\x00' * sector_size * 4)

        # === Backup boot sector (sector 6) ===
        f.write(boot)
        # Backup FSInfo (sector 7)
        f.write(fsinfo)

        # Remaining reserved sectors (8 through reserved-1)
        remaining = reserved_sectors - 8
        f.write(b'\x00' * sector_size * remaining)

        # === FAT tables ===
        # Build FAT in memory
        fat = bytearray(fat_sectors * sector_size)
        # Entry 0: media descriptor
        struct.pack_into('<I', fat, 0, 0x0FFFFFF8)
        # Entry 1: end of chain marker
        struct.pack_into('<I', fat, 4, 0x0FFFFFFF)
        # Entry 2: root directory (end of chain for now)
        struct.pack_into('<I', fat, 8, 0x0FFFFFFF)

        # Allocate clusters for files and write data
        next_cluster = 3  # First available cluster

        # Collect directory structure
        dir_entries: dict[str, list[tuple[str, int, int, bytes]]] = {"": []}
        # Each entry: (name, first_cluster, size, is_dir)
        file_data: dict[int, bytes] = {}  # cluster -> data

        # Create directory entries
        dirs_created: set[str] = set()
        for img_path in sorted(files.keys()):
            parts = PurePosixPath(img_path).parts
            # Create parent directories
            for i in range(1, len(parts)):
                d = "/".join(parts[:i])
                if d not in dirs_created:
                    parent = "/".join(parts[:i-1])
                    dir_name = parts[i-1]
                    # Allocate cluster for directory
                    dir_cluster = next_cluster
                    struct.pack_into('<I', fat, dir_cluster * 4, 0x0FFFFFFF)
                    next_cluster += 1
                    if parent not in dir_entries:
                        dir_entries[parent] = []
                    dir_entries[parent].append((dir_name, dir_cluster, 0, True))
                    dir_entries[d] = []
                    dirs_created.add(d)

            # Add file entry
            parent_dir = "/".join(parts[:-1])
            file_name = parts[-1]
            local_path = files[img_path]
            file_bytes = open(local_path, "rb").read()
            file_size = len(file_bytes)

            if file_size == 0:
                if parent_dir not in dir_entries:
                    dir_entries[parent_dir] = []
                dir_entries[parent_dir].append((file_name, 0, 0, False))
                continue

            first_cluster = next_cluster
            # Allocate clusters for file data
            clusters_needed = (file_size + spc * sector_size - 1) // (spc * sector_size)
            for c in range(clusters_needed):
                cluster_num = next_cluster
                next_cluster += 1
                if c < clusters_needed - 1:
                    struct.pack_into('<I', fat, cluster_num * 4, cluster_num + 1)
                else:
                    struct.pack_into('<I', fat, cluster_num * 4, 0x0FFFFFFF)
                # Store data for this cluster
                offset = c * spc * sector_size
                chunk = file_bytes[offset:offset + spc * sector_size]
                file_data[cluster_num] = chunk

            if parent_dir not in dir_entries:
                dir_entries[parent_dir] = []
            dir_entries[parent_dir].append((file_name, first_cluster, file_size, False))

        # Update FSInfo with correct free cluster count and next free cluster
        used_clusters = next_cluster - 2  # clusters 2..next_cluster-1
        free_clusters = cluster_count - used_clusters
        struct.pack_into('<I', fsinfo, 488, free_clusters)
        struct.pack_into('<I', fsinfo, 492, next_cluster)
        # Write updated FSInfo at sector 1 and backup at sector 7
        part_base = partition_offset * sector_size
        f.seek(part_base + 1 * sector_size)
        f.write(fsinfo)
        f.seek(part_base + 7 * sector_size)
        f.write(fsinfo)
        # Seek to FAT start
        f.seek(part_base + reserved_sectors * sector_size)

        # Write FAT1 and FAT2
        f.write(fat)
        f.write(fat)

        # === Data area ===
        # Write clusters sequentially
        total_data_clusters = next_cluster - 2  # clusters 2..next_cluster-1
        bytes_per_cluster = spc * sector_size

        # Track used 8.3 names per directory to avoid collisions
        used_short_names: dict[str, set[str]] = {}  # dir_path -> set of "BASEEXT"

        def _to_short_name(name: str, is_dir: bool, dir_path: str) -> tuple[str, str]:
            """Convert a long filename to unique 8.3 format with ~N tails."""
            if is_dir:
                base = name.upper()[:8]
                ext = ""
            else:
                parts = name.rsplit(".", 1)
                base = parts[0].upper()[:8]
                ext = parts[1].upper()[:3] if len(parts) > 1 else ""

            # Strip invalid characters
            base = "".join(c if c.isalnum() or c in "_-" else "_" for c in base)
            ext = "".join(c if c.isalnum() or c in "_-" else "_" for c in ext)

            if dir_path not in used_short_names:
                used_short_names[dir_path] = set()

            candidate = (base.ljust(8)[:8] + ext.ljust(3)[:3])
            if candidate not in used_short_names[dir_path]:
                used_short_names[dir_path].add(candidate)
                return base.ljust(8)[:8], ext.ljust(3)[:3]

            # Collision — use ~N suffix
            for n in range(1, 1000):
                suffix = f"~{n}"
                max_base = 8 - len(suffix)
                short_base = base[:max_base] + suffix
                candidate = (short_base.ljust(8)[:8] + ext.ljust(3)[:3])
                if candidate not in used_short_names[dir_path]:
                    used_short_names[dir_path].add(candidate)
                    return short_base.ljust(8)[:8], ext.ljust(3)[:3]

            # Shouldn't happen
            return base.ljust(8)[:8], ext.ljust(3)[:3]

        def _lfn_checksum(short_name: bytes) -> int:
            """Compute the 8.3 name checksum used by LFN entries."""
            chk = 0
            for b in short_name[:11]:
                chk = ((chk >> 1) + ((chk & 1) << 7) + b) & 0xFF
            return chk

        def _make_lfn_entries(long_name: str, short_name_11: bytes) -> bytes:
            """Create VFAT Long File Name directory entries.

            Returns N * 32 bytes (LFN entries in reverse order, ready to
            prepend before the 8.3 entry).
            """
            chksum = _lfn_checksum(short_name_11)
            # Encode name as UTF-16LE, add null terminator
            ucs2 = bytearray(long_name.encode("utf-16-le"))
            ucs2 += b'\x00\x00'  # null terminator
            # Pad to multiple of 26 bytes (13 chars * 2 bytes each)
            while len(ucs2) % 26 != 0:
                ucs2 += b'\xff\xff'

            num_entries = len(ucs2) // 26
            entries = bytearray()

            for i in range(num_entries):
                entry = bytearray(32)
                seq = i + 1
                if i == num_entries - 1:
                    seq |= 0x40  # last LFN entry marker
                entry[0] = seq
                entry[11] = 0x0F  # LFN attribute
                entry[13] = chksum

                # 13 UTF-16LE chars (26 bytes) split across three fields
                chunk = ucs2[i * 26:(i + 1) * 26]
                entry[1:11] = chunk[0:10]    # chars 1-5
                entry[14:26] = chunk[10:22]  # chars 6-11
                entry[28:32] = chunk[22:26]  # chars 12-13

                entries += entry

            # LFN entries are stored in reverse order (highest seq first)
            result = bytearray()
            for i in range(num_entries - 1, -1, -1):
                result += entries[i * 32:(i + 1) * 32]
            return bytes(result)

        def _needs_lfn(name: str, base8: str, ext3: str, is_dir: bool) -> bool:
            """Check if the long name differs from the 8.3 short name."""
            if is_dir:
                short = base8.rstrip()
                return name.upper() != short
            parts = name.rsplit(".", 1)
            short = base8.rstrip() + ("." + ext3.rstrip() if ext3.rstrip() else "")
            return name.upper() != short

        def _make_dir_entry(name: str, cluster: int, size: int,
                            is_dir: bool, dir_path: str = "") -> bytes:
            """Create FAT32 directory entry(s) with LFN support.

            Returns LFN entries (if needed) + 32-byte 8.3 entry.
            """
            base8, ext3 = _to_short_name(name, is_dir, dir_path)
            short_name_11 = (base8.encode("ascii", errors="replace") +
                             ext3.encode("ascii", errors="replace"))

            # 8.3 short entry
            entry = bytearray(32)
            entry[0:8] = base8.encode("ascii", errors="replace")
            entry[8:11] = ext3.encode("ascii", errors="replace")
            entry[11] = 0x10 if is_dir else 0x20  # Attribute
            struct.pack_into('<H', entry, 20, cluster >> 16)  # High cluster
            struct.pack_into('<H', entry, 26, cluster & 0xFFFF)  # Low cluster
            struct.pack_into('<I', entry, 28, size)

            # Add LFN entries if the long name differs from 8.3
            if _needs_lfn(name, base8, ext3, is_dir):
                lfn = _make_lfn_entries(name, short_name_11)
                return lfn + bytes(entry)
            return bytes(entry)

        def _make_label_entry(label_bytes: bytes) -> bytes:
            """Create a volume label directory entry."""
            entry = bytearray(32)
            entry[0:11] = label_bytes
            entry[11] = 0x08  # Volume label attribute
            return bytes(entry)

        def _make_dot_entry(name: str, cluster: int) -> bytes:
            """Create a '.' or '..' directory entry."""
            entry = bytearray(32)
            padded = name.ljust(11)[:11]
            entry[0:11] = padded.encode("ascii")
            entry[11] = 0x10  # Directory attribute
            struct.pack_into('<H', entry, 20, cluster >> 16)
            struct.pack_into('<H', entry, 26, cluster & 0xFFFF)
            return bytes(entry)

        # Build a map of dir_path -> cluster number
        dir_cluster_map: dict[str, int] = {"": 2}  # root = cluster 2
        for dir_path, entries in dir_entries.items():
            for name, cl, sz, isd in entries:
                if isd:
                    child = f"{dir_path}/{name}" if dir_path else name
                    dir_cluster_map[child] = cl

        # Build cluster data for directories
        for dir_path, entries in dir_entries.items():
            cluster_num = dir_cluster_map.get(dir_path)
            if cluster_num is None:
                continue

            dir_data = bytearray()
            if dir_path == "":
                # Volume label entry in root (no . or .. in root)
                dir_data += _make_label_entry(vol_label_bytes)
            else:
                # . entry (points to self)
                dir_data += _make_dot_entry(".", cluster_num)
                # .. entry (points to parent, 0 if parent is root)
                parent = "/".join(dir_path.split("/")[:-1])
                parent_cluster = dir_cluster_map.get(parent, 0)
                # FAT spec: .. in root's children should be 0
                if parent_cluster == 2:
                    parent_cluster = 0
                dir_data += _make_dot_entry("..", parent_cluster)

            for name, cl, sz, isd in entries:
                dir_data += _make_dir_entry(name, cl, sz, isd, dir_path)

            # Split dir_data across clusters if it exceeds one cluster
            num_dir_clusters = max(1, -(-len(dir_data) // bytes_per_cluster))
            # Pad to fill last cluster
            padded_len = num_dir_clusters * bytes_per_cluster
            dir_data += b'\x00' * (padded_len - len(dir_data))

            # First cluster is already allocated; allocate extras
            dir_clusters = [cluster_num]
            for _ in range(num_dir_clusters - 1):
                dir_clusters.append(next_cluster)
                next_cluster += 1

            # Chain directory clusters in FAT
            for i, dcl in enumerate(dir_clusters):
                if i < len(dir_clusters) - 1:
                    struct.pack_into('<I', fat, dcl * 4, dir_clusters[i + 1])
                else:
                    struct.pack_into('<I', fat, dcl * 4, 0x0FFFFFFF)

            # Write each cluster
            for i, dcl in enumerate(dir_clusters):
                chunk = dir_data[i * bytes_per_cluster:(i + 1) * bytes_per_cluster]
                file_data[dcl] = bytes(chunk)

        # Write all clusters from 2 to next_cluster-1
        for cluster_num in range(2, max(next_cluster, 3)):
            if cluster_num in file_data:
                data = file_data[cluster_num]
                if len(data) < bytes_per_cluster:
                    data += b'\x00' * (bytes_per_cluster - len(data))
                f.write(data[:bytes_per_cluster])
            else:
                f.write(b'\x00' * bytes_per_cluster)

        # Fill remaining space to reach total size
        current_pos = f.tell()
        target_size = size_mb * 1024 * 1024
        if current_pos < target_size:
            f.write(b'\x00' * (target_size - current_pos))

    cfg.log(f"  Copied {len(files)} files to image")


def _build_img_windows(cfg: object, files: dict[str, str], output: str,
                       mbr: bool = False) -> None:
    """Create FAT32 image on Windows — pure Python, no admin needed.

    Uses _write_fat32_image() to write FAT32 structures directly,
    bypassing diskpart/VHD entirely (like Rufus's approach).
    """
    from mkimage import PartitionSpec

    part = cfg.partitions[0] if cfg.partitions else PartitionSpec()
    content_mb = _calculate_content_size(files)
    size_mb = _interpret_size(part.size, content_mb)

    cfg.log(f"  Image size: {size_mb}MB ({content_mb}MB content + {size_mb - content_mb}MB free)")
    cfg.log(f"  {len(files)} files ({content_mb * 1024}KB) to include")

    _write_fat32_image(cfg, files, output, size_mb, part.label[:11],
                       part.cluster_size, mbr=mbr)

    fmt = "FAT32 MBR" if mbr else "FAT32"
    actual_size = os.path.getsize(output)
    cfg.log(f"  [OK] Created {output} ({actual_size // 1024}KB, {fmt})")

    if cfg.verify:
        _verify_write(cfg, files, output)


def build_img(cfg: object, files: dict[str, str], output: str) -> None:
    """Create a FAT32 disk image (no partition table).

    Primary method uses mcopy (no root needed). Falls back to mount+rsync
    if mcopy is unavailable and root access is available.
    On Windows, delegates to mkimage.ps1 for native image creation.
    """
    if _is_windows():
        _build_img_windows(cfg, files, output)
        return

    from mkimage import PartitionSpec

    ensure_tools(cfg, "img")
    out = _resolve(output)

    part = cfg.partitions[0] if cfg.partitions else PartitionSpec()
    content_mb = _calculate_content_size(files)
    size_mb = _interpret_size(part.size, content_mb)
    part_label = part.label[:11]
    cfg.log(f"  Image size: {size_mb}MB ({content_mb}MB content + {size_mb - content_mb}MB free)")

    with tempfile.TemporaryDirectory() as staging:
        _stage_files(files, Path(staging))
        stg_resolved = _resolve(staging)

        if _which("mcopy"):
            # Primary: mcopy (no root needed)
            cfg.log(f"  Copying {len(files)} files to image...")
            dd_cmd = ["dd", "if=/dev/zero", f"of={out}", "bs=1M", f"count={size_mb}"]
            if cfg.verbose:
                dd_cmd.append("status=progress")
            _run(cfg, dd_cmd, verbose=True)
            mkfs_cmd = ["mkfs.vfat", "-F", "32", "-n", part_label]
            if part.cluster_size > 0:
                mkfs_cmd.extend(["-s", str(part.cluster_size // 512)])
            mkfs_cmd.append(out)
            _run(cfg, mkfs_cmd, verbose=True)
            _populate_img_mcopy(cfg, files, out)
        elif _which("rsync"):
            # Fallback: mount+rsync (requires root)
            cfg.log("  mcopy not found, using mount+rsync (requires root)...")
            cfg.log(f"  Copying {len(files)} files to image...")
            if not _populate_img_mount(cfg, stg_resolved, out, size_mb, part_label):
                raise RuntimeError(
                    "mount/rsync failed. Install mtools for rootless operation:\n"
                    f"    {_suggest_install('mcopy')}"
                )
        else:
            raise RuntimeError(
                "No file copy tool available. Install mtools:\n"
                f"    {_suggest_install('mcopy')}"
            )

    actual_size = os.path.getsize(output)
    cfg.log(f"  [OK] Created {output} ({actual_size // 1024}KB, FAT32)")

    if cfg.verify:
        _verify_write(cfg, files, output)
