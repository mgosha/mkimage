"""List contents of FAT32 disk images (raw or MBR-partitioned)."""
from __future__ import annotations

import os
import struct
from pathlib import Path


def _find_fat32_offset(data: bytes) -> int:
    """Find the byte offset where the FAT32 filesystem starts.

    Returns 0 for raw FAT32 images, or the partition offset for MBR images.
    """
    # Check for MBR: boot signature at 510-511, partition entry at 446
    if len(data) >= 512 and data[510] == 0x55 and data[511] == 0xAA:
        part_type = data[450]
        lba_start = struct.unpack_from('<I', data, 454)[0]
        # FAT32 LBA types: 0x0B, 0x0C
        if part_type in (0x0B, 0x0C) and lba_start > 0:
            return lba_start * 512
    return 0


def _read_short_name(entry: bytes) -> str:
    """Decode an 8.3 directory entry name."""
    base = entry[0:8].decode("ascii", errors="replace").rstrip()
    ext = entry[8:11].decode("ascii", errors="replace").rstrip()
    if ext:
        return f"{base}.{ext}"
    return base


def _get_cluster_chain(fat: bytes, start: int) -> list[int]:
    """Follow a FAT32 cluster chain."""
    chain = []
    cluster = start
    while 2 <= cluster < 0x0FFFFFF8:
        chain.append(cluster)
        if len(chain) > 100000:  # Safety limit
            break
        next_val = struct.unpack_from('<I', fat, cluster * 4)[0] & 0x0FFFFFFF
        cluster = next_val
    return chain


def list_image(image_path: str, log: object = print) -> list[dict[str, object]]:
    """List files in a FAT32 disk image.

    Supports raw FAT32 and MBR-partitioned images.
    Returns a list of dicts with 'path', 'size', 'is_dir' keys.
    """
    if not os.path.isfile(image_path):
        log(f"Error: {image_path} not found")
        return []

    file_size = os.path.getsize(image_path)
    with open(image_path, "rb") as f:
        # Read enough to detect format
        header = f.read(min(file_size, 4096))

    fat_offset = _find_fat32_offset(header)

    with open(image_path, "rb") as f:
        f.seek(fat_offset)
        bpb = f.read(512)

        # Validate BPB
        if bpb[510] != 0x55 or bpb[511] != 0xAA:
            log(f"Error: No valid FAT32 filesystem found in {image_path}")
            return []

        sector_size = struct.unpack_from('<H', bpb, 11)[0]
        spc = bpb[13]
        reserved = struct.unpack_from('<H', bpb, 14)[0]
        num_fats = bpb[16]
        total_sectors = struct.unpack_from('<I', bpb, 32)[0]
        fat_size = struct.unpack_from('<I', bpb, 36)[0]
        root_cluster = struct.unpack_from('<I', bpb, 44)[0]
        label = bpb[71:82].decode("ascii", errors="replace").rstrip()

        if sector_size == 0 or spc == 0:
            log(f"Error: Invalid BPB in {image_path}")
            return []

        bytes_per_cluster = spc * sector_size
        fat_start = fat_offset + reserved * sector_size
        data_start = fat_offset + (reserved + num_fats * fat_size) * sector_size

        # Read FAT
        f.seek(fat_start)
        fat = f.read(fat_size * sector_size)

        # Print image info
        total_bytes = total_sectors * sector_size
        log(f"Image: {image_path}")
        if fat_offset > 0:
            log(f"  Partition table: MBR (FAT32 at sector {fat_offset // 512})")
        else:
            log(f"  Partition table: None (raw FAT32)")
        log(f"  Label: {label}")
        log(f"  Size: {total_bytes // (1024*1024)}MB "
            f"({sector_size}B sectors, {bytes_per_cluster}B clusters)")
        log(f"  Files:")

        # Walk directory tree
        results: list[dict[str, object]] = []

        def _read_cluster_data(cluster_num: int) -> bytes:
            offset = data_start + (cluster_num - 2) * bytes_per_cluster
            f.seek(offset)
            return f.read(bytes_per_cluster)

        def _walk_dir(dir_cluster: int, prefix: str) -> None:
            chain = _get_cluster_chain(fat, dir_cluster)
            dir_data = b""
            for c in chain:
                dir_data += _read_cluster_data(c)

            for i in range(0, len(dir_data), 32):
                entry = dir_data[i:i+32]
                if len(entry) < 32:
                    break
                if entry[0] == 0x00:  # End of directory
                    break
                if entry[0] == 0xE5:  # Deleted
                    continue
                attr = entry[11]
                if attr == 0x0F:  # Long filename entry
                    continue
                if attr & 0x08:  # Volume label
                    continue

                name = _read_short_name(entry)
                if name in (".", ".."):
                    continue

                cluster_hi = struct.unpack_from('<H', entry, 20)[0]
                cluster_lo = struct.unpack_from('<H', entry, 26)[0]
                first_cluster = (cluster_hi << 16) | cluster_lo
                file_size_val = struct.unpack_from('<I', entry, 28)[0]
                is_dir = bool(attr & 0x10)

                full_path = f"{prefix}{name}" if not prefix else f"{prefix}/{name}"

                if is_dir:
                    results.append({"path": full_path + "/", "size": 0, "is_dir": True})
                    log(f"    {full_path}/")
                    if first_cluster >= 2:
                        _walk_dir(first_cluster, full_path)
                else:
                    results.append({"path": full_path, "size": file_size_val, "is_dir": False})
                    size_str = _format_size(file_size_val)
                    log(f"    {full_path}  ({size_str})")

        _walk_dir(root_cluster, "")

        if not results:
            log("    (empty)")

        # Summary
        file_count = sum(1 for r in results if not r["is_dir"])
        total_content = sum(r["size"] for r in results if not r["is_dir"])
        log(f"  Total: {file_count} files, {_format_size(total_content)}")

        return results


def _format_size(size: int) -> str:
    """Format a byte count as a human-readable string."""
    if size >= 1024 * 1024:
        return f"{size / (1024*1024):.1f}MB"
    if size >= 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size}B"
