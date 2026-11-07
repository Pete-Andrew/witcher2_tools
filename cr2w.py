# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal Witcher 2 CR2W (version <= 115) reader, plus "un-inlining".

Cooked Witcher 2 entity templates often contain whole resources (meshes,
skeletons) inlined as object subtrees: a root object whose 'link' field holds
the resource's original depot path (e.g. ...\\model\\arachas__legs1_b1.w2mesh),
followed by its child objects (materials and so on). Those original files do
not exist anywhere else in the game archives.

uninline() writes each such subtree back out as a standalone CR2W file at its
original depot path. Object indices are preserved (objects outside the
subtree become empty placeholders), so internal object references and the
import table stay valid without any re-numbering.

File layout (v115): 'CR2W', u32 version, u32 flags, then four tables of
(u32 offset, u32 count): names, objects, imports, dependencies. Object
entries: u16 class name index (1-based), u32 parent (1-based, 0 = root),
u32 data size, u32 data offset (absolute), u32 flags, u32 unknown, then a
length-prefixed link string.
"""

import os
import struct

PLACEHOLDER_CLASS = "CObject"
BAD_LEAD_BYTES = (0x01, 0x80)
UNINLINE_EXTS = (".w2mesh", ".w2rig")
# Inlined resources a carved mesh may point at internally (a material
# instance's base shader graph is often inlined too); kept in every carve.
SHARED_EXTS = (".w2mg", ".w2mi")


def _read_str(b, p):
    """CR2W v115 string: first byte bit7 = 8-bit chars (else UTF-16),
    bit6 = one extra length byte follows (adds 64 x its value), bits0-5 = length."""
    head = b[p]
    if head == 0x80:
        return "", p + 1
    wide = not (head & 0x80)
    n = head & 0x3F
    q = p + 1
    if head & 0x40:
        n += 64 * b[q]
        q += 1
    size = n * 2 if wide else n
    raw = b[q:q + size]
    text = raw.decode("utf-16le" if wide else "latin1").rstrip("\x00")
    return text, q + size


def _write_str(s):
    raw = s.encode("latin1")
    n = len(raw)
    if n == 0:
        return b"\x80"
    if n < 64:
        return bytes([0x80 | n]) + raw
    if n >= 64 * 256:
        raise ValueError("String too long for CR2W: %d" % n)
    return bytes([0x80 | 0x40 | (n % 64), n // 64]) + raw


class CR2WObject:
    __slots__ = ("index", "cls", "cls_idx", "parent", "size", "offset", "flags", "unk", "link")


class CR2WFile:
    def __init__(self, data):
        if data[:4] != b"CR2W":
            raise ValueError("Not a CR2W file")
        self.data = data
        self.version, self.flags = struct.unpack_from("<II", data, 4)
        if self.version > 115:
            raise ValueError("Not a Witcher 2 file (version %d)" % self.version)
        self.tables = [struct.unpack_from("<II", data, 12 + 8 * i) for i in range(4)]
        (n_off, n_cnt), (o_off, o_cnt), (i_off, i_cnt), (d_off, d_cnt) = self.tables

        self.names = []
        p = n_off
        for _ in range(n_cnt):
            s, p = _read_str(data, p)
            self.names.append(s)
        self.names_end = p

        self.objects = []
        p = o_off
        for i in range(o_cnt):
            o = CR2WObject()
            o.index = i + 1
            o.cls_idx, o.parent, o.size, o.offset, o.flags, o.unk = struct.unpack_from("<HIIIII", data, p)
            p += 22
            o.link, p = _read_str(data, p)
            o.cls = self.names[o.cls_idx - 1] if 0 < o.cls_idx <= len(self.names) else "?"
            self.objects.append(o)

    def section(self, table):
        """Raw bytes of a table, from its offset to the start of the next section."""
        off, cnt = self.tables[table]
        if cnt == 0:
            return b""
        starts = sorted({t[0] for t in self.tables if t[1]} | {min(o.offset for o in self.objects) if self.objects else len(self.data)})
        later = [s for s in starts if s > off]
        end = min(later) if later else len(self.data)
        return self.data[off:end]

    def subtree(self, root):
        """Indices (1-based) of root and all its descendants."""
        children = {}
        for o in self.objects:
            children.setdefault(o.parent, []).append(o.index)
        out, todo = [], [root]
        while todo:
            i = todo.pop()
            out.append(i)
            todo.extend(children.get(i, ()))
        return sorted(out)

    def inlined_roots(self, exts=UNINLINE_EXTS):
        return [o for o in self.objects if o.parent == 0 and o.link and o.link.lower().endswith(exts)]

    def _handle_props(self, body):
        """Yield (value offset in body, value) for top-level handle properties."""
        p = 0
        n = len(body)
        while p + 10 <= n:
            name_idx, type_idx = struct.unpack_from("<HH", body, p)
            if name_idx == 0:
                return
            size = struct.unpack_from("<I", body, p + 6)[0]
            if size < 4 or p + 6 + size > n:
                return
            tname = self.names[type_idx - 1] if 0 < type_idx <= len(self.names) else ""
            if tname.startswith(("*", "#")) and size == 8:
                yield p + 10, struct.unpack_from("<i", body, p + 10)[0]
            p += 6 + size

    def _top_props(self, body):
        """Spans [(start, end)] of an object's top-level properties, or None if unparsable."""
        spans = []
        p = 0
        n = len(body)
        while p + 2 <= n:
            name_idx = struct.unpack_from("<H", body, p)[0]
            if name_idx == 0:
                return spans
            if p + 10 > n:
                return None
            size = struct.unpack_from("<I", body, p + 6)[0]
            if size < 4 or p + 6 + size > n:
                return None
            spans.append((p, p + 6 + size))
            p += 6 + size
        return None

    def _safe_lead(self, body, alias_name):
        """Witcher 3 Tools reads an object's first byte as a marker: 0x01 makes it skip
        4 bytes and 0x80 read a packed integer, so it loses its place and drops every
        property. An object's first two bytes are its first property's name number,
        so when the low byte is one of those, point it at a duplicate of the same
        name appended to the name table. alias_name(old_index) returns that index."""
        if len(body) < 2 or body[0] not in BAD_LEAD_BYTES:
            return body
        old = struct.unpack_from("<H", body, 0)[0]
        if not (0 < old <= len(self.names)):
            return body
        new = alias_name(old)
        if new is None:
            return body
        return struct.pack("<H", new) + body[2:]

    def build_standalone(self, root):
        """Return bytes of a standalone CR2W containing root's subtree at original indices."""
        keep = set(self.subtree(root.index))
        if root.cls == "CMesh":
            for shared in self.inlined_roots(SHARED_EXTS):
                keep.update(self.subtree(shared.index))
        last = max(keep)

        names_raw = self.data[self.tables[0][0]:self.names_end]
        names_cnt = len(self.names)
        if PLACEHOLDER_CLASS in self.names:
            ph_idx = self.names.index(PLACEHOLDER_CLASS) + 1
        else:
            names_raw += _write_str(PLACEHOLDER_CLASS)
            names_cnt += 1
            ph_idx = names_cnt

        imports_raw = bytearray(self.section(2))
        import_count = self.tables[2][1]
        deps_raw = self.section(3)

        # Material instances whose base shader is inlined point at it by object
        # index; importers expect a file reference, so turn those into imports.
        patches = {}  # object index -> patched body
        new_imports = {}  # inlined object index -> import number
        for idx in sorted(keep):
            o = self.objects[idx - 1]
            if o.cls != "CMaterialInstance":
                continue
            body = bytearray(self.data[o.offset:o.offset + o.size])
            changed = False
            for pos, value in list(self._handle_props(bytes(body))):
                if not (0 < value <= len(self.objects)):
                    continue
                target = self.objects[value - 1]
                path = target.link
                if not path or not path.lower().endswith(SHARED_EXTS):
                    continue
                raw = path.encode("latin1") + b"\x00"
                if len(raw) >= 64:
                    continue  # keep the in-file reference rather than risk a bad import entry
                if value not in new_imports:
                    cls_idx = target.cls_idx
                    imports_raw += bytes([len(raw)]) + raw + struct.pack("<HH", cls_idx, 0)
                    import_count += 1
                    new_imports[value] = import_count
                struct.pack_into("<i", body, pos, -new_imports[value])
                changed = True
            if changed:
                patches[idx] = bytes(body)
        imports_raw = bytes(imports_raw)

        # Duplicate names appended for _safe_lead; appending never shifts existing indices.
        aliases = {}
        extra_names = []

        def alias_name(old):
            if old in aliases:
                return aliases[old]
            while True:
                new = names_cnt + len(extra_names) + 1
                if new > 0xFFFF:
                    return None
                if (new & 0xFF) not in BAD_LEAD_BYTES:
                    break
                extra_names.append(PLACEHOLDER_CLASS)  # filler to skip an unusable index
            extra_names.append(self.names[old - 1])
            aliases[old] = new
            return new

        entries, blobs = [], []
        for o in self.objects[:last]:
            if o.index in keep:
                link = "" if o.index == root.index else o.link
                body = patches.get(o.index) or self.data[o.offset:o.offset + o.size]
                body = self._safe_lead(bytes(body), alias_name)
                entries.append((o.cls_idx, o.parent, o.flags, o.unk, link))
            else:
                body = b"\x00\x00"  # empty property list
                entries.append((ph_idx, 0, 0, 0, ""))
            blobs.append(body)

        for s in extra_names:
            names_raw += _write_str(s)
        names_cnt += len(extra_names)

        header_size = 12 + 8 * 4
        names_off = header_size
        imports_off = names_off + len(names_raw)
        deps_off = imports_off + len(imports_raw)
        objects_off = deps_off + len(deps_raw)

        table_size = sum(22 + len(_write_str(e[4])) for e in entries)
        data_off = objects_off + table_size

        # The root resource's data goes physically last. Witcher 3 Tools reads a
        # CMesh's vertex buffers from the end of its properties to the end of the
        # file, so anything stored after the mesh (shared material graphs with a
        # higher object index) breaks the import. Offsets are absolute, so the
        # object numbering is unaffected.
        root_pos = root.index - 1
        order = [i for i in range(len(entries)) if i != root_pos] + [root_pos]
        offsets = [0] * len(entries)
        pos = data_off
        for i in order:
            offsets[i] = pos
            pos += len(blobs[i])

        table = bytearray()
        for i, ((cls_idx, parent, flags, unk, link), body) in enumerate(zip(entries, blobs)):
            table += struct.pack("<HIIIII", cls_idx, parent, len(body), offsets[i], flags, unk)
            table += _write_str(link)

        header = b"CR2W" + struct.pack("<II", self.version, self.flags)
        header += struct.pack("<II", names_off, names_cnt)
        header += struct.pack("<II", objects_off, len(entries))
        header += struct.pack("<II", imports_off if import_count else 0, import_count)
        header += struct.pack("<II", deps_off if self.tables[3][1] else 0, self.tables[3][1])

        return header + names_raw + imports_raw + deps_raw + bytes(table) + b"".join(blobs[i] for i in order)


def _write_replace(dst, blob):
    """Atomically replace dst. On Windows a file another reader left open cannot be
    replaced; Witcher 3 Tools leaks handles when an import fails, so collect those
    first and retry before giving up."""
    import gc
    tmp = dst + ".part"
    with open(tmp, "wb") as w:
        w.write(blob)
    for attempt in range(3):
        try:
            os.replace(tmp, dst)
            return True
        except PermissionError:
            gc.collect()
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


def uninline(data, data_root, overwrite=False):
    """Write every inlined mesh/skeleton in a CR2W file to data_root/<link path>.

    Returns a list of (depot path lower-case, absolute path) written or already present.
    """
    try:
        f = CR2WFile(data)
    except Exception:
        return []
    out = []
    for root in f.inlined_roots():
        rel = root.link.replace("/", "\\")
        dst = os.path.join(data_root, rel)
        exists = os.path.exists(dst)
        if overwrite or not exists:
            try:
                blob = f.build_standalone(root)
            except Exception as e:
                print("[Witcher 2 Tools] could not carve %s: %s" % (rel, e))
                if exists:
                    out.append((rel.lower(), dst))
                continue
            if exists:
                with open(dst, "rb") as r:
                    if r.read() == blob:
                        out.append((rel.lower(), dst))
                        continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not _write_replace(dst, blob):
                print("[Witcher 2 Tools] could not update %s (file in use); using the existing copy" % rel)
        out.append((rel.lower(), dst))
    return out
