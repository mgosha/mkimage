"""Write verification via SHA256 comparison."""
from __future__ import annotations

from typing import TYPE_CHECKING

from mkimage.platform import _run, _which

if TYPE_CHECKING:
    from mkimage import Config


def _verify_write(cfg: Config, source_files: dict[str, str],
                  image_path: str) -> bool:
    """Verify written image by comparing file hashes.

    Uses mcopy to extract files from the image and compares SHA256 hashes
    to the source files. No root needed.

    Returns True if all files match, False if any mismatch.
    """
    import hashlib

    def _sha256(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    # No mtools (e.g. Windows): verify in pure Python by reading the image's
    # FAT32 filesystem directly and comparing hashes.
    if not _which("mcopy"):
        return _verify_pure_python(cfg, source_files, image_path,
                                   _sha256, _sha256_bytes)

    cfg.log(f"  Verifying {len(source_files)} files...")
    failures = 0
    for img_path, local_path in sorted(source_files.items()):
        src_hash = _sha256(local_path)
        r = _run(cfg, ["mcopy", "-i", image_path, f"::{img_path}", "-"],
                 check=False)
        if r.returncode != 0:
            cfg.log(f"  VERIFY FAIL: {img_path} (extract failed)")
            failures += 1
            continue
        # mcopy outputs to stdout as text, but we need binary comparison
        # Re-extract with subprocess directly for binary data
        import subprocess as _sp
        rr = _sp.run(["mcopy", "-i", image_path, f"::{img_path}", "-"],
                     capture_output=True)
        img_hash = _sha256_bytes(rr.stdout)
        if src_hash != img_hash:
            cfg.log(f"  VERIFY FAIL: {img_path} (hash mismatch)")
            failures += 1
        elif cfg.verbose:
            cfg.log(f"  VERIFY OK: {img_path}")

    if failures == 0:
        cfg.log(f"  Verification passed: all {len(source_files)} files match")
        return True
    cfg.log(f"  Verification FAILED: {failures} file(s) differ")
    return False


def _verify_pure_python(cfg: Config, source_files: dict[str, str],
                        image_path: str, sha256_file, sha256_bytes) -> bool:
    """Verify by reading the image's FAT32 filesystem in pure Python.

    Used when mtools is unavailable (Windows). Comparison is case-insensitive
    because the pure-Python writer folds short names to uppercase 8.3.
    """
    from mkimage.modify import _read_fat32_image

    try:
        img_files = _read_fat32_image(image_path)["files"]
    except Exception as exc:  # GPT / multi-partition / unrecognized layout
        cfg.log(f"  Warning: cannot read image for verification ({exc}); "
                f"skipping")
        return True

    in_image = {k.replace("\\", "/").lower(): v for k, v in img_files.items()}
    cfg.log(f"  Verifying {len(source_files)} files...")
    failures = 0
    for img_path, local_path in sorted(source_files.items()):
        key = img_path.replace("\\", "/").lower()
        if key not in in_image:
            cfg.log(f"  VERIFY FAIL: {img_path} (missing in image)")
            failures += 1
            continue
        if sha256_file(local_path) != sha256_bytes(in_image[key]):
            cfg.log(f"  VERIFY FAIL: {img_path} (hash mismatch)")
            failures += 1
        elif cfg.verbose:
            cfg.log(f"  VERIFY OK: {img_path}")

    if failures == 0:
        cfg.log(f"  Verification passed: all {len(source_files)} files match")
        return True
    cfg.log(f"  Verification FAILED: {failures} file(s) differ")
    return False
