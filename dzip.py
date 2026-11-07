# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only access to Witcher 2 .dzip archives.

Never writes to the game folder. All archives in CookedPC are opened;
pack0.dzip is loaded first and every other archive (patches, DLC, mods)
overrides it in alphabetical order, matching how later archives replace
earlier files.
"""

import os
import struct

CHUNK = 0x10000


def lzf_decompress(src):
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        ctrl = src[i]
        i += 1
        if ctrl < 32:
            length = ctrl + 1
            out += src[i:i + length]
            i += length
        else:
            length = ctrl >> 5
            ref = len(out) - ((ctrl & 0x1F) << 8) - 1
            if length == 7:
                length += src[i]
                i += 1
            ref -= src[i]
            i += 1
            length += 2
            if ref < 0:
                raise ValueError("Corrupt LZF data (back-reference out of range)")
            if ref + length <= len(out):
                out += out[ref:ref + length]
            else:
                for k in range(length):
                    out.append(out[ref + k])
    return bytes(out)


class Dzip:
    def __init__(self, path):
        self.path = path
        self.entries = {}  # lower name -> (name, size, offset, csize)
        with open(path, "rb") as f:
            hdr = f.read(32)
            if len(hdr) < 32 or hdr[:4] != b"DZIP":
                raise ValueError("Not a DZIP archive: %s" % path)
            _ver, count, _unk, toff, _hash = struct.unpack("<IIIQQ", hdr[4:32])
            f.seek(toff)
            table = f.read()
        pos = 0
        for _ in range(count):
            n = struct.unpack_from("<H", table, pos)[0]
            pos += 2
            name = table[pos:pos + n].rstrip(b"\0").decode("latin1")
            pos += n
            _ts, size, off, csize = struct.unpack_from("<QQQQ", table, pos)
            pos += 32
            self.entries[name.lower()] = (name, size, off, csize)

    def read(self, key):
        name, size, off, csize = self.entries[key.lower()]
        if size == 0:
            return b""
        with open(self.path, "rb") as f:
            f.seek(off)
            blob = f.read(csize)
        nchunks = struct.unpack_from("<I", blob, 0)[0] // 4
        offs = list(struct.unpack_from("<%dI" % nchunks, blob, 0)) + [csize]
        out = bytearray()
        for c in range(nchunks):
            out += lzf_decompress(blob[offs[c]:offs[c + 1]])
        if len(out) != size:
            raise ValueError("Size mismatch for %s (%d != %d)" % (name, len(out), size))
        return bytes(out)


class DzipSet:
    """All archives of one Witcher 2 install, merged into one index."""

    def __init__(self, cooked_dir):
        self.cooked_dir = cooked_dir
        self.index = {}  # lower name -> (Dzip, name)
        self.errors = []
        files = [f for f in os.listdir(cooked_dir) if f.lower().endswith(".dzip")]
        files.sort(key=lambda f: (f.lower() != "pack0.dzip", f.lower()))
        for f in files:
            try:
                dz = Dzip(os.path.join(cooked_dir, f))
            except Exception as e:  # damaged or foreign archive: skip, keep going
                self.errors.append("%s: %s" % (f, e))
                continue
            for key, entry in dz.entries.items():
                self.index[key] = (dz, entry[0])

    def __contains__(self, key):
        return key.lower() in self.index

    def name(self, key):
        return self.index[key.lower()][1]

    def read(self, key):
        dz, _ = self.index[key.lower()]
        return dz.read(key)

    def keys(self):
        return self.index.keys()

    def extract(self, key, data_root):
        """Extract one file into data_root (skips if already cached). Returns its path."""
        dz, name = self.index[key.lower()]
        out = os.path.join(data_root, name)
        size = dz.entries[key.lower()][1]
        if os.path.exists(out) and os.path.getsize(out) == size:
            return out
        data = dz.read(key)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + ".part"
        with open(tmp, "wb") as w:
            w.write(data)
        os.replace(tmp, out)
        return out


_CACHE = {}


def get_set(cooked_dir):
    key = os.path.normcase(os.path.abspath(cooked_dir))
    if key not in _CACHE:
        _CACHE[key] = DzipSet(cooked_dir)
    return _CACHE[key]


def clear_sets():
    _CACHE.clear()
