# SPDX-License-Identifier: GPL-3.0-or-later
"""Witcher 2 .xbm (CR2W CBitmapTexture) to .dds conversion.

An .xbm holds DXT-compressed mipmaps without a DDS header. The mip table is
u32 count followed by, per mip: u32 width, u32 height, u32 unknown,
u32 byte size, then the data. This adds a standard DDS header in front.
"""

import os
import struct

_POW2 = {1 << n for n in range(0, 14)}


def _blocks(w, h):
    return max(1, (w + 3) // 4) * max(1, (h + 3) // 4)


def read_mips(data):
    for i in range(16, min(len(data), 16384) - 20):
        cnt, w, h, _x, sz = struct.unpack_from("<IIIII", data, i)
        if not (1 <= cnt <= 16 and w in _POW2 and h in _POW2 and w >= 4 and h >= 4):
            continue
        if sz not in (_blocks(w, h) * 8, _blocks(w, h) * 16):
            continue
        mips = []
        p = i + 4
        ok = True
        for _ in range(cnt):
            if p + 16 > len(data):
                ok = False
                break
            mw, mh, _mx, msz = struct.unpack_from("<IIII", data, p)
            if msz <= 0 or p + 16 + msz > len(data):
                ok = False
                break
            mips.append((mw, mh, data[p + 16:p + 16 + msz]))
            p += 16 + msz
        if ok and mips:
            return mips
    return None


def _dds_header(w, h, mip_count, fourcc, linear_size):
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000 | (0x20000 if mip_count > 1 else 0)
    caps = 0x1000 | (0x400008 if mip_count > 1 else 0)
    pixel_format = struct.pack("<II4sIIIII", 32, 0x4, fourcc, 0, 0, 0, 0, 0)
    return (b"DDS " + struct.pack("<IIIIIII", 124, flags, h, w, linear_size, 0, mip_count)
            + b"\0" * 44 + pixel_format + struct.pack("<IIIII", caps, 0, 0, 0, 0))


def convert(xbm_path, dds_path=None):
    """Convert one .xbm to .dds next to it. Returns (width, height, mips, format)."""
    if dds_path is None:
        dds_path = os.path.splitext(xbm_path)[0] + ".dds"
    with open(xbm_path, "rb") as f:
        data = f.read()
    mips = read_mips(data)
    if not mips:
        raise ValueError("No readable mip table in %s" % os.path.basename(xbm_path))
    w, h, first = mips[0]
    fourcc = b"DXT1" if len(first) == _blocks(w, h) * 8 else b"DXT5"
    tmp = dds_path + ".part"
    with open(tmp, "wb") as f:
        f.write(_dds_header(w, h, len(mips), fourcc, len(first)))
        for _, _, m in mips:
            f.write(m)
    os.replace(tmp, dds_path)
    return w, h, len(mips), fourcc.decode()


def ensure_dds(xbm_path):
    dds = os.path.splitext(xbm_path)[0] + ".dds"
    if os.path.exists(dds) and os.path.getmtime(dds) >= os.path.getmtime(xbm_path):
        return dds
    convert(xbm_path, dds)
    return dds
