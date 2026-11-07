# SPDX-License-Identifier: GPL-3.0-or-later
"""Monster catalogue and import pipeline.

Mesh and rig parsing is done by the Witcher 3 Tools add-on (its Witcher 2
support). This module works out which files an entity needs, extracts and
converts them into a cache laid out as <cache>/data/..., points Witcher 3
Tools at that cache for the duration of the import only, then binds every
mesh to the entity's full skeleton.
"""

import math
import os
import re

import bpy

from . import cr2w, xbm

# Witcher 2 keeps monster entities in three places: standalone models,
# in-game spawn entities, and the appearance templates that hold the meshes.
# (prefix, index of the path part that names the monster type, source label)
MONSTER_ROOTS = (
    ("characters\\monsters\\", 2, "model"),
    ("game\\gameplay\\monsters\\", 3, "gameplay"),
    ("templates\\characters\\appearances\\monsters\\", 4, "appearance"),
)
CHARACTER_PREFIX = "characters\\"

# Characters tab: (prefix, index of the path part used as the group, fixed group or
# None, source label). The group is what the list shows on the right.
CHARACTER_ROOTS = (
    ("game\\npc\\", None, "Main characters", "gameplay"),
    ("templates\\characters\\appearances\\main npc\\", None, "Main characters", "appearance"),
    ("characters\\main_npc\\", None, "Main characters", "model"),
    ("templates\\characters\\appearances\\crowd\\", None, "Crowd & soldiers", "appearance"),
    ("characters\\npc\\", None, "Crowd & soldiers", "model"),
    # The game files human enemies (bandits, Kaedwen soldiers) under monsters.
    ("game\\gameplay\\monsters\\bandits\\", None, "Crowd & soldiers", "gameplay"),
    ("characters\\templates\\witcher\\witcher.w2ent", None, "Geralt", "template"),
)

# Human enemies the game stores under its monster folders; shown under Characters.
_HUMAN_MONSTER_PREFIXES = ("game\\gameplay\\monsters\\bandits\\",)

# Other tab: animals and other non-character creatures (same tuple layout).
OTHER_ROOTS = (
    ("templates\\characters\\appearances\\animals\\", None, "Animals", "appearance"),
    ("characters\\animals\\", None, "Animals", "model"),
)

# Entities that are effects, markers or helpers and contain no model of their own.
_NO_MODEL_STEMS = ("mappin", "bubble", "_slots", "intervention_position", "trajectories",
                   "_explode")

_REF_RE = re.compile(rb"[A-Za-z0-9_\\ .\-]+\.(?:w2mesh|w2rig|w2ent|w2mi|xbm)")

# Meshes left out of a monster import:
#  - editor/debug placeholders (engine\meshes\editor\debug.w2mesh is a cube)
#  - "veins" effect overlays with their own separate rig (shown in-game on hit)
_PLACEHOLDER_PREFIXES = ("engine\\", "characters\\common\\veins\\")


def _is_placeholder(key):
    return key.startswith(_PLACEHOLDER_PREFIXES) or os.path.basename(key).startswith("fx_veins")

MAX_RIG_CANDIDATES = 4
BIND_TOLERANCE_MM = 5.0
BIND_MIN_FRACTION = 0.9


# ---------------------------------------------------------------- catalogue

def _pretty(stem):
    for suffix in ("__appearances", "_appearances", "_appearance", "_appeariences"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def build_catalog(dset, kind="MONSTERS"):
    """Return (group, display name, archive path) for every listable entity.

    kind is "MONSTERS", "CHARACTERS" or "OTHER". The display name ends with the source in
    brackets, because the same creature often exists as a model, an appearance
    template (all variants) and a gameplay spawn.
    """
    items = []
    for key in dset.keys():
        if not key.endswith(".w2ent"):
            continue
        parts = key.split("\\")
        stem = os.path.splitext(parts[-1])[0]
        if any(s in stem for s in _NO_MODEL_STEMS):
            continue
        if kind in ("CHARACTERS", "OTHER"):
            roots = CHARACTER_ROOTS if kind == "CHARACTERS" else OTHER_ROOTS
            for prefix, _idx, group, source in roots:
                if key.startswith(prefix):
                    items.append((group, "%s  (%s)" % (_pretty(stem), source), dset.name(key)))
                    break
            continue
        if key.startswith(_HUMAN_MONSTER_PREFIXES):
            continue
        for prefix, type_idx, source in MONSTER_ROOTS:
            if key.startswith(prefix):
                group = parts[type_idx] if len(parts) > type_idx + 1 else ""
                items.append((group, "%s  (%s)" % (_pretty(stem), source), dset.name(key)))
                break
    # Shortened names must stay unique (kaedwen_elite.w2ent and
    # kaedwen_elite_appearances.w2ent would both read "kaedwen_elite").
    counts = {}
    for group, label, path in items:
        counts[(group, label)] = counts.get((group, label), 0) + 1
    unique = []
    for group, label, path in items:
        if counts[(group, label)] > 1:
            stem = os.path.splitext(os.path.basename(path))[0]
            label = "%s  (%s" % (stem, label.split("  (", 1)[1])
        unique.append((group, label, path))
    unique.sort(key=lambda t: (t[0].lower(), t[1].lower()))
    return unique


# ------------------------------------------------------------- dependencies

def find_refs(data, dset):
    """Return the archive keys referenced by a CR2W file (strings it contains)."""
    found = set()
    for m in _REF_RE.findall(data):
        s = m.decode("latin1").lower().strip()
        for i in range(0, min(6, len(s))):
            if s[i:] in dset:
                found.add(s[i:])
                break
    return found


def _local_path(dset, key, data_root):
    """Path of a resource in the cache: carved files already there, archive files extracted."""
    key = key.lower()
    if key in dset:
        return dset.extract(key, data_root)
    path = os.path.join(data_root, key)
    return path if os.path.exists(path) else None


def _extract_with_textures(dset, key, data_root, done):
    """Make sure a mesh/material file is in the cache, plus the textures it references."""
    key = key.lower()
    if key in done:
        return
    done.add(key)
    path = _local_path(dset, key, data_root)
    if path is None:
        return
    if key.endswith(".xbm"):
        try:
            xbm.ensure_dds(path)
        except Exception as e:
            print("[Witcher 2 Tools] texture skipped: %s (%s)" % (key, e))
        return
    if key.endswith((".w2mesh", ".w2mi")):
        with open(path, "rb") as f:
            data = f.read()
        for ref in find_refs(data, dset):
            if ref.endswith((".xbm", ".w2mi")):
                _extract_with_textures(dset, ref, data_root, done)


def _entity_parts(dset, key, data_root):
    """Extract one entity, un-inline any embedded meshes/skeletons, and list what it uses."""
    path = dset.extract(key, data_root)
    with open(path, "rb") as f:
        data = f.read()
    refs = find_refs(data, dset)
    # Always re-carve: it is quick, and keeps the cache in step with this add-on version.
    carved = {rel for rel, _ in cr2w.uninline(data, data_root, overwrite=True)}
    found = refs | carved
    meshes = {r for r in found if r.endswith(".w2mesh") and not _is_placeholder(r)}
    rigs = {r for r in found if r.endswith(".w2rig")}
    return refs, meshes, rigs


_ITEM_TEMPLATES = {}
_ITEM_TAG = re.compile(r"<item\b([^>]*)>")
_ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def item_templates(dset):
    """Map inventory item name -> its equipment template entity (archive key).

    Some characters (Triss, for one) get their body as an inventory item, named
    in the entity and defined in items\\def_item_*.xml; the item's equip_template
    is the stem of an entity under items\\ that holds the meshes.
    """
    cached = _ITEM_TEMPLATES.get(id(dset))
    if cached is not None:
        return cached
    stems = {}
    for k in dset.keys():
        if k.startswith("items\\") and k.endswith(".w2ent"):
            stems.setdefault(os.path.splitext(os.path.basename(k))[0], k)
    table = {}
    for k in dset.keys():
        if not (k.startswith("items\\def_item") and k.endswith(".xml")):
            continue
        try:
            raw = dset.read(k)
        except Exception:
            continue
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or (len(raw) > 1 and raw[1:2] == b"\x00"):
            text = raw.decode("utf-16", "ignore")
        else:
            text = raw.decode("utf-8", "ignore")
        for m in _ITEM_TAG.finditer(text):
            attrs = {a.lower(): v for a, v in _ATTR.findall(m.group(1))}
            name, tpl = attrs.get("name"), attrs.get("equip_template")
            if name and tpl and tpl.lower() in stems:
                table[name.lower()] = stems[tpl.lower()]
    _ITEM_TEMPLATES[id(dset)] = table
    return table


def _body_items(dset, key, data_root):
    """Equipment templates for the '<name>_body' items an entity carries."""
    path = dset.extract(key, data_root)
    with open(path, "rb") as f:
        data = f.read()
    try:
        names = cr2w.CR2WFile(data).names
    except Exception:
        return []
    table = item_templates(dset)
    out = []
    for n in names:
        low = n.lower()
        if low.endswith("_body") and low in table and table[low] not in out:
            out.append(table[low])
    return out


def collect_entity(dset, ent_key, data_root):
    """Prepare an entity and everything needed to import it. Returns (meshes, rig paths) as absolute paths."""
    refs, meshes, rigs = _entity_parts(dset, ent_key, data_root)
    sources = [ent_key]

    # Gameplay entities usually include an appearance template that holds the meshes.
    if not meshes:
        for sub in sorted(r for r in refs if r.endswith(".w2ent") and r != ent_key.lower()):
            if sub.startswith(("items\\", "fx\\")):
                continue
            _sub_refs, sub_meshes, sub_rigs = _entity_parts(dset, sub, data_root)
            meshes |= sub_meshes
            rigs |= sub_rigs
            sources.append(sub)

    # Bodies supplied as inventory items.
    for src in sources:
        for item_ent in _body_items(dset, src, data_root):
            _r, item_meshes, _rigs = _entity_parts(dset, item_ent, data_root)
            meshes |= item_meshes

    done = set()
    for m in sorted(meshes):
        _extract_with_textures(dset, m, data_root, done)

    # Candidate body skeletons, best guess first. Face-animation rigs (mimics,
    # float tracks) are never the body skeleton. import_entity tries each and
    # keeps the one the meshes actually fit.
    stem = os.path.splitext(os.path.basename(ent_key))[0].lower()
    body_rigs = [r for r in rigs if "mimic" not in r and "floattrack" not in r]
    ordered = sorted(body_rigs, key=lambda r: (stem not in os.path.basename(r), r))
    rig_paths = [p for p in (_local_path(dset, r, data_root) for r in ordered[:MAX_RIG_CANDIDATES]) if p]

    mesh_paths = [p for p in (_local_path(dset, m, data_root) for m in sorted(meshes)) if p]
    return mesh_paths, rig_paths


# ---------------------------------------------------------- Witcher 3 Tools

def find_w3_prefs(context):
    for _name, addon in context.preferences.addons.items():
        prefs = getattr(addon, "preferences", None)
        if prefs is not None and hasattr(prefs, "witcher2_game_path"):
            return prefs
    return None


def w3_tools_status(context):
    """Return '' when Witcher 3 Tools is usable, otherwise a message."""
    ops = getattr(bpy.ops, "witcher", None)
    needed = ("import_w2mesh", "import_w2_rig")
    if ops is None or not all(hasattr(ops, n) for n in needed):
        return "Witcher 3 Tools add-on not found or too old (needs W2 mesh and rig import)"
    if find_w3_prefs(context) is None:
        return "Witcher 3 Tools has no 'Witcher 2 Path' setting (update it)"
    return ""


class _RedirectW2Path:
    """Temporarily point Witcher 3 Tools at the cache, restoring it afterwards."""

    def __init__(self, prefs, cache_root):
        self.prefs = prefs
        self.cache_root = cache_root
        self.old = None

    def __enter__(self):
        self.old = self.prefs.witcher2_game_path
        self.prefs.witcher2_game_path = self.cache_root
        return self

    def __exit__(self, *exc):
        self.prefs.witcher2_game_path = self.old
        return False


# ------------------------------------------------------------------ binding

def _new_objects(before):
    return [bpy.data.objects[n] for n in set(bpy.data.objects.keys()) - before]


def _bone_errors(rig, arm):
    """[(mm, bone name)] distance between each of arm's bones and the same bone in rig."""
    out = []
    for b in arm.data.bones:
        rb = rig.data.bones.get(b.name)
        if rb is not None:
            p = arm.matrix_world @ b.head_local
            q = rig.matrix_world @ rb.head_local
            out.append(((p - q).length * 1000.0, b.name))
    return out


def _median(values):
    values = sorted(values)
    return values[len(values) // 2] if values else None


def _choose_rig_rotation(rig, local_arms):
    """Rigs import facing either way; keep whichever orientation matches the meshes best.

    Judged by the median bone error, so a few bones posed differently (eyes,
    jaw) cannot outvote the rest of the skeleton.
    """
    best = None
    for angle in (0.0, math.pi):
        rig.rotation_euler.z = angle
        bpy.context.view_layer.update()
        errs = [e for la in local_arms for e, _ in _bone_errors(rig, la)]
        if not errs:
            return
        med = _median(errs)
        if best is None or med < best[1]:
            best = (angle, med)
    rig.rotation_euler.z = best[0]
    bpy.context.view_layer.update()


def _conform_rest_pose(context, rig, local_arms):
    """Rebuild the skeleton's rest pose from the meshes' bind pose.

    Some Witcher 2 skeletons (bullvore, troll, endriaga) store a reference pose
    that differs from the pose their meshes were skinned in, so no rigid
    transform lines them up. Bones the meshes use take their exact bind matrix;
    every other bone keeps its offset from its parent. Returns how many bones
    were set from mesh data.
    """
    import mathutils
    bind = {}
    for la in local_arms:
        for b in la.data.bones:
            if b.name not in bind:
                bind[b.name] = la.matrix_world @ b.matrix_local

    rig_world = rig.matrix_world.copy()
    old_world = {b.name: rig_world @ b.matrix_local for b in rig.data.bones}
    lengths = {b.name: b.length for b in rig.data.bones}
    order, todo = [], [b for b in rig.data.bones if b.parent is None]
    while todo:
        b = todo.pop(0)
        order.append(b.name)
        todo.extend(b.children)

    parents = {b.name: (b.parent.name if b.parent else None) for b in rig.data.bones}
    new_world = {}
    for name in order:
        if name in bind:
            new_world[name] = bind[name]
        else:
            par = parents[name]
            if par is None:
                new_world[name] = old_world[name]
            else:
                local = old_world[par].inverted() @ old_world[name]
                new_world[name] = new_world[par] @ local

    prev_active = context.view_layer.objects.active
    prev_mode = context.mode
    if prev_mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for o in context.selected_objects:
        o.select_set(False)
    rig.hide_set(False)
    rig.select_set(True)
    context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    inv = rig_world.inverted()
    ebones = rig.data.edit_bones
    for name in order:
        eb = ebones.get(name)
        if eb is None:
            continue
        eb.matrix = inv @ new_world[name]
        if lengths.get(name):
            eb.length = lengths[name]
    bpy.ops.object.mode_set(mode="OBJECT")
    rig.select_set(False)
    context.view_layer.objects.active = prev_active
    return sum(1 for n in order if n in bind)


def _matches(errors):
    """A mesh armature matches the skeleton when nearly all its bones line up."""
    if not errors:
        return False
    within = sum(1 for e, _ in errors if e <= BIND_TOLERANCE_MM)
    return within / len(errors) >= BIND_MIN_FRACTION and _median([e for e, _ in errors]) <= 1.0


def _mesh_images(objects):
    seen = {}
    for o in objects:
        if o.type != "MESH":
            continue
        for mat in o.data.materials:
            if not mat or not mat.node_tree:
                continue
            for node in mat.node_tree.nodes:
                img = getattr(node, "image", None)
                if img is not None:
                    seen[img.name] = img
    return list(seen.values())


def _relink_images(objects, data_root):
    """Point every texture at its converted .dds in the cache and load it.

    Returns (loaded, total). Blender loads images lazily, so each one is
    reloaded and judged by its pixel size rather than has_data.
    """
    images = _mesh_images(objects)
    loaded = 0
    for img in images:
        path = bpy.path.abspath(img.filepath)
        if not os.path.exists(path):
            low = path.lower().replace("/", "\\")
            idx = low.rfind("\\data\\")
            if idx >= 0:
                cand = os.path.join(data_root, os.path.splitext(path[idx + 6:])[0] + ".dds")
                if os.path.exists(cand):
                    img.filepath = cand
        try:
            img.reload()
        except Exception:
            pass
        if img.size[0] > 0:
            loaded += 1
    return loaded, len(images)


def _layer_collection(layer_coll, coll):
    if layer_coll.collection == coll:
        return layer_coll
    for child in layer_coll.children:
        found = _layer_collection(child, coll)
        if found:
            return found
    return None


# ------------------------------------------------------------------ import

def import_entity(context, dset, ent_key, cache_root, *, materials=True, lods=False):
    """Import one Witcher 2 entity. Returns (root_object, report_lines)."""
    report = []
    data_root = os.path.join(cache_root, "data")
    os.makedirs(data_root, exist_ok=True)

    w3 = find_w3_prefs(context)
    if w3 is None:
        raise RuntimeError(w3_tools_status(context) or "Witcher 3 Tools not available")

    meshes, rig_paths = collect_entity(dset, ent_key, data_root)
    if not meshes:
        raise RuntimeError("No meshes referenced by %s" % ent_key)

    name = os.path.splitext(os.path.basename(ent_key))[0]
    coll = bpy.data.collections.new(name)
    context.scene.collection.children.link(coll)
    lc = _layer_collection(context.view_layer.layer_collection, coll)
    prev_lc = context.view_layer.active_layer_collection
    if lc:
        context.view_layer.active_layer_collection = lc

    rig = None
    rig_candidates = []
    try:
        with _RedirectW2Path(w3, cache_root):
            for rp in rig_paths:
                before = set(bpy.data.objects.keys())
                try:
                    bpy.ops.witcher.import_w2_rig(filepath=rp)
                except Exception as e:
                    report.append("Skeleton failed: %s (%s)" % (os.path.basename(rp), str(e)[-120:]))
                    continue
                new = _new_objects(before)
                arms = [o for o in new if o.type == "ARMATURE"]
                for o in new:
                    if o not in arms[:1]:
                        bpy.data.objects.remove(o, do_unlink=True)
                if arms:
                    rig_candidates.append(arms[0])

            mesh_objs, local_arms = [], []
            for m in meshes:
                before = set(bpy.data.objects.keys())
                try:
                    bpy.ops.witcher.import_w2mesh(
                        filepath=m,
                        do_import_mats=materials,
                        do_import_armature=True,
                        keep_lod_meshes=lods,
                    )
                except Exception as e:
                    report.append("Mesh failed: %s (%s)" % (os.path.basename(m), e))
                    continue
                for o in _new_objects(before):
                    (mesh_objs if o.type == "MESH" else local_arms).append(o)
    finally:
        context.view_layer.active_layer_collection = prev_lc

    local_arms = [a for a in local_arms if a.type == "ARMATURE"]

    # Several skeletons can be referenced (a dwarf entity also names the human
    # rig, for example). Keep whichever the most meshes line up with.
    if rig_candidates:
        def score(cand):
            if not local_arms:
                return (0, 0)
            _choose_rig_rotation(cand, local_arms)
            errs = [_bone_errors(cand, la) for la in local_arms]
            fits = sum(1 for e in errs if e and _matches(e))
            shared = sum(len(e) for e in errs)
            return (fits, shared)
        scored = [(score(c), i, c) for i, c in enumerate(rig_candidates)]
        scored.sort(key=lambda t: (t[0], -t[1]), reverse=True)
        rig = scored[0][2]
        for _s, _i, c in scored[1:]:
            data = c.data
            bpy.data.objects.remove(c, do_unlink=True)
            if data.users == 0:
                bpy.data.armatures.remove(data)
        if len(rig_candidates) > 1:
            report.append("Chose skeleton %s from %d candidates" % (rig.name, len(rig_candidates)))

    if not mesh_objs:
        # Nothing usable came in: remove the empty skeleton/collection and say why.
        for o in ([rig] if rig else []) + local_arms:
            bpy.data.objects.remove(o, do_unlink=True)
        if not coll.objects:
            bpy.data.collections.remove(coll)
        detail = "; ".join(report) or "the mesh reader returned nothing"
        raise RuntimeError("%d mesh file(s) found but none could be read (%s)" % (len(meshes), detail[:300]))
    root = rig

    conformed = 0
    if rig is not None and local_arms:
        _choose_rig_rotation(rig, local_arms)
        errors = {la.name: _bone_errors(rig, la) for la in local_arms}
        if not all(_matches(e) for e in errors.values() if e):
            # Some meshes don't fit the skeleton's stored pose. Sometimes that pose
            # simply differs from the one the meshes were skinned in (bullvore,
            # Zoltan, bruxa) and rebuilding it from the meshes fixes everything;
            # sometimes the misfits are extra variants (draug) and rebuilding would
            # make the good ones worse. Try the rebuild on a copy and keep whichever
            # skeleton more mesh bones fit.
            def fit_score(r):
                return sum(len(la.data.bones) for la in local_arms
                           if _matches(_bone_errors(r, la)))
            before_score = fit_score(rig)
            trial = rig.copy()
            trial.data = rig.data.copy()
            for c in rig.users_collection:
                c.objects.link(trial)
            trial.rotation_euler.z = 0.0
            context.view_layer.update()
            n_conformed = _conform_rest_pose(context, trial, local_arms)
            context.view_layer.update()
            if fit_score(trial) > before_score:
                old, old_data = rig, rig.data
                old_name = old.name
                bpy.data.objects.remove(old, do_unlink=True)
                trial.name = old_name
                if old_data.users == 0:
                    bpy.data.armatures.remove(old_data)
                rig = trial
                root = rig
                conformed = n_conformed
            else:
                trial_data = trial.data
                bpy.data.objects.remove(trial, do_unlink=True)
                if trial_data.users == 0:
                    bpy.data.armatures.remove(trial_data)
            errors = {la.name: _bone_errors(rig, la) for la in local_arms}
        bound, kept, all_errs = 0, 0, []
        for mo in mesh_objs:
            la = mo.parent if mo.parent is not None and mo.parent.type == "ARMATURE" else None
            if la is None:
                la = next((md.object for md in mo.modifiers if md.type == "ARMATURE" and md.object), None)
            errs = errors.get(la.name) if la is not None else None
            if not errs or not _matches(errs):
                kept += 1
                continue
            mw = mo.matrix_world.copy()
            mo.parent = rig
            mo.matrix_parent_inverse = rig.matrix_world.inverted()
            mo.matrix_world = mw
            for md in mo.modifiers:
                if md.type == "ARMATURE":
                    md.object = rig
            all_errs.extend(errs)
            bound += 1
        remaining = []
        for la in local_arms:
            in_use = any(c for c in la.children) or any(
                md.object == la for o in mesh_objs for md in o.modifiers if md.type == "ARMATURE")
            if in_use:
                remaining.append(la)
            else:
                data = la.data
                bpy.data.objects.remove(la, do_unlink=True)
                if data.users == 0:
                    bpy.data.armatures.remove(data)
        if bound:
            values = [e for e, _ in all_errs]
            offset = sorted({n for e, n in all_errs if e > BIND_TOLERANCE_MM})
            rig["w2_bind_error_mm"] = round(_median(values), 4)
            rig["w2_bind_bones_within_5mm"] = "%d/%d" % (len(values) - sum(1 for v in values if v > BIND_TOLERANCE_MM), len(values))
            line = "Bound %d of %d mesh(es) to %d-bone skeleton (median bone error %.3f mm" % (
                bound, len(mesh_objs), len(rig.data.bones), _median(values))
            if conformed:
                rig["w2_rest_pose_from_meshes"] = conformed
                line += "; rest pose rebuilt from mesh bind pose for %d bones" % conformed
            if offset:
                rig["w2_bind_bones_offset"] = ", ".join(offset)
                line += "; %d bone(s) posed differently, up to %.0f mm: %s" % (
                    len(offset), max(values), ", ".join(offset[:6]))
            report.append(line + ")")
        else:
            rig.rotation_euler.z = 0.0
        if kept:
            report.append("%d mesh(es) did not line up with the skeleton and keep their own armatures" % kept)
            if not bound and remaining:
                root = remaining[0]
    elif rig is None and local_arms:
        root = local_arms[0]
        report.append("No full skeleton referenced; meshes kept on their own armatures")

    if rig is not None:
        rig.name = name

    if materials:
        loaded, total = _relink_images(mesh_objs, data_root)
        report.append("Textures loaded: %d/%d" % (loaded, total))
        if rig is not None:
            rig["w2_textures_loaded"] = "%d/%d" % (loaded, total)

    # Imported objects sometimes land outside the active collection; gather them.
    for o in set(mesh_objs) | set(a for a in [rig] if a):
        if coll not in o.users_collection:
            for c in list(o.users_collection):
                c.objects.unlink(o)
            coll.objects.link(o)

    return root, report
