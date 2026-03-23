"""File compression and decompression utilities."""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from mkimage.platform import _run, _which

if TYPE_CHECKING:
    from mkimage import Config


def _is_compressed_path(path: str) -> bool:
    """Check if path has a compression extension."""
    return any(path.endswith(ext) for ext in (".gz", ".zst", ".xz"))


def _strip_compression_ext(path: str) -> str:
    """Strip compression extension: 'foo.img.gz' -> 'foo.img'."""
    for ext in (".gz", ".zst", ".xz"):
        if path.endswith(ext):
            return path[:-len(ext)]
    return path


def _compress_file(cfg: Config, input_path: str, output_path: str) -> None:
    """Compress a file. Detects format from output extension."""
    import gzip as _gzip
    import lzma as _lzma

    cfg.log(f"  Compressing to {Path(output_path).name}...")
    if output_path.endswith(".gz"):
        with open(input_path, "rb") as fin, _gzip.open(output_path, "wb") as fout:
            while True:
                chunk = fin.read(1024 * 1024)
                if not chunk:
                    break
                fout.write(chunk)
    elif output_path.endswith(".xz"):
        with open(input_path, "rb") as fin, _lzma.open(output_path, "wb") as fout:
            while True:
                chunk = fin.read(1024 * 1024)
                if not chunk:
                    break
                fout.write(chunk)
    elif output_path.endswith(".zst"):
        if not _which("zstd"):
            raise RuntimeError("zstd not found. Install zstd or use .gz/.xz")
        _run(cfg, ["zstd", "-f", "-o", output_path, input_path], verbose=True)
    else:
        raise ValueError(f"Unknown compression format: {output_path}")

    in_size = os.path.getsize(input_path)
    out_size = os.path.getsize(output_path)
    ratio = out_size / in_size * 100 if in_size > 0 else 0
    cfg.log(f"  Compressed: {in_size // 1024}KB -> {out_size // 1024}KB ({ratio:.0f}%)")


def _decompress_pipe_cmd(source_path: str) -> list[str]:
    """Return decompression command for piping to dd."""
    if source_path.endswith(".gz"):
        return ["gzip", "-dc", source_path]
    if source_path.endswith(".xz"):
        return ["xz", "-dc", source_path]
    if source_path.endswith(".zst"):
        return ["zstd", "-dc", source_path]
    return ["cat", source_path]
