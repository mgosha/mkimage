# Third-Party Tools and Licensing

mkimage itself is MIT-licensed (see `LICENSE`). It does **not** bundle any
third-party tool in the public distribution. Two optional, externally
licensed helpers are fetched **on demand** at runtime, only when a feature
that needs them is used. Because the *user's* machine downloads them directly
from upstream, mkimage never redistributes them and no third-party license
obligations attach to mkimage's own MIT release.

This document records the licensing of those tools so the on-demand model is
deliberate and any future decision to bundle is made with the terms in hand.

---

## fat32format (Ridgecrop)

Used on Windows to create a **whole-disk FAT32** volume on drives larger than
32GB — Windows' built-in formatter refuses FAT32 above 32GB. Invoked by
`mkimage.ps1` as a separate process; downloaded on demand (see
`Get-Fat32Format` in `mkimage.ps1`). Falls back to a 32GB-capped FAT32
partition if unavailable.

- **Author / copyright:** Tom Thornhill, Ridgecrop Consultants Ltd.
  (`// (c) Tom Thornhill 2007,2008,2009` in `fat32format.c`).
- **License:** **GPL** (version unspecified — the source header and the binary
  both say only "covered by the GPL"; no `LICENSE`/COPYING file is shipped in
  the binary zip).
- **Author's explicit grant** (http://ridgecrop.co.uk/fat32format.htm):
  > "It is licensed under the GPL license - you may distribute source and
  > binaries. You can build it into an open source application. If you want to
  > build it into a closed source application you should approach me for
  > licensing it under a BSD style license for a fee."

### Redistribution is permitted (it was previously assumed not to be)
mkimage is open source (MIT), so the author's "build it into an open source
application" grant applies. mkimage runs `fat32format.exe` as a **separate
process** (not linked), so this is *mere aggregation*: mkimage's own code
stays MIT and is unaffected; only `fat32format.exe` remains under the GPL.

### If a build bundles `fat32format.exe`, it must:
1. Provide the **corresponding source** — `fat32formatsrc.zip` (see URLs
   below). The `Release/fat32format.exe` inside that source zip is
   **byte-identical** to the standalone binary (same SHA256), so shipping the
   source zip alongside satisfies the GPL "provide source" requirement.
2. Include the GPL license text and preserve the
   `(c) Tom Thornhill` + GPL notice.
3. Note the tool and its license in the build's documentation.

### Why the public build does NOT bundle it
Download-on-demand keeps mkimage's MIT release free of any GPL component:
the user fetches the binary from Ridgecrop, mkimage never redistributes it,
and no GPL obligations attach. Bundling is reserved for an **internal** build
where the network blocks the upstream URL — and such a build must follow the
three requirements above.

### Upstream references (pinned)
- Page: `http://ridgecrop.co.uk/fat32format.htm`
  (note: host is `ridgecrop.co.uk` — **no** `www.`; the `www.` host 404s.
  HTTPS uses a self-signed cert, so the download is plain HTTP.)
- Binary (~20K): `http://ridgecrop.co.uk/download/fat32format.zip`
  — SHA256 `812a33f01c7d73a1e4b89427c01b6bf967dc8d8ef3671200f381b130356b3068`
- Source + binary (~30K): `http://ridgecrop.co.uk/download/fat32formatsrc.zip`
  — SHA256 `d1f350f277d14c92d7a0984d9566dd8baa2592e6b37c63ca657217f251116989`
- `fat32format.exe` v1.07
  — SHA256 `d5320a127374af23139730f0d01aee8195e5fe15b63c35d48d80930abbf7f5cb`

Override the download source / verify it via `MKIMAGE_FAT32FORMAT_URL` and
`MKIMAGE_FAT32FORMAT_SHA256` (see `mkimage.ps1`).

---

## UEFI:NTFS (pbatard/uefi-ntfs)

Used to boot NTFS USB partitions under UEFI. Downloaded on demand from the
project's GitHub releases (see `mkimage/uefi_ntfs.py`); not bundled.

- **License:** GPL-2.0 (github.com/pbatard/uefi-ntfs).
- Same rationale as above: on-demand download keeps it out of mkimage's MIT
  release. If ever bundled, ship/offer the corresponding source per GPL-2.0.
