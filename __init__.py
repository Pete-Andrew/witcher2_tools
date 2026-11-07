# SPDX-License-Identifier: GPL-3.0-or-later
"""Witcher 2 Tools: browse and import textured Witcher 2 monsters.

Requires your own copy of The Witcher 2 and the Witcher 3 Tools add-on
(which provides the Witcher 2 mesh and rig readers).
"""

import os
import re
import sys
import subprocess

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, IntProperty,
                       StringProperty)

from . import dzip, importer

bl_info = {
    "name": "Witcher 2 Tools",
    "author": "Witcher 2 Tools contributors",
    "version": (0, 2, 2),
    "blender": (4, 5, 0),
    "location": "3D Viewport > Sidebar > Witcher 2",
    "description": "Browse and import textured Witcher 2 monsters and characters from your own game files",
    "category": "Import-Export",
}

PKG = __package__


# ------------------------------------------------------------------ helpers

def get_prefs(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(PKG)
    return addon.preferences if addon else None


def default_cache_dir():
    try:
        return bpy.utils.extension_path_user(PKG, path="cache", create=True)
    except Exception:
        return bpy.utils.user_resource("DATAFILES", path="witcher2_tools_cache", create=True)


def cache_dir(context=None):
    prefs = get_prefs(context)
    path = bpy.path.abspath(prefs.cache_path) if prefs and prefs.cache_path else ""
    return path or default_cache_dir()


def cooked_dir(game_path):
    if not game_path:
        return ""
    game_path = bpy.path.abspath(game_path)
    for name in ("CookedPC", "cookedpc"):
        p = os.path.join(game_path, name)
        if os.path.isfile(os.path.join(p, "pack0.dzip")):
            return p
    if os.path.isfile(os.path.join(game_path, "pack0.dzip")):
        return game_path
    return ""


def _steam_libraries():
    roots = []
    candidates = [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam",
                  os.path.expanduser("~/.steam/steam"), os.path.expanduser("~/.local/share/Steam")]
    for steam in candidates:
        vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
        if os.path.isfile(vdf):
            roots.append(steam)
            try:
                with open(vdf, encoding="utf-8", errors="ignore") as f:
                    for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                        roots.append(m.group(1).replace("\\\\", "\\"))
            except OSError:
                pass
    return roots


def auto_detect_game_path():
    cands = []
    for lib in _steam_libraries():
        cands.append(os.path.join(lib, "steamapps", "common", "the witcher 2"))
    for drive in "CDEFGH":
        cands += [drive + r":\GOG Games\The Witcher 2",
                  drive + r":\Program Files (x86)\GOG Galaxy\Games\The Witcher 2",
                  drive + r":\SteamLibrary\steamapps\common\the witcher 2"]
    for c in cands:
        if cooked_dir(c):
            return c
    return ""


def get_dset(context):
    prefs = get_prefs(context)
    cdir = cooked_dir(prefs.game_path if prefs else "")
    if not cdir:
        return None
    return dzip.get_set(cdir)


# --------------------------------------------------------------- properties

class W2T_CatalogItem(bpy.types.PropertyGroup):
    group: StringProperty()
    path: StringProperty()


def _on_game_path(self, context):
    dzip.clear_sets()


class W2T_Preferences(bpy.types.AddonPreferences):
    bl_idname = PKG

    game_path: StringProperty(
        name="Witcher 2 Folder",
        description="Your Witcher 2 install folder (the one containing CookedPC). Only read, never modified",
        subtype="DIR_PATH",
        update=_on_game_path,
    )
    cache_path: StringProperty(
        name="Cache Folder",
        description="Where extracted and converted files are kept. Leave empty for the default. "
                    "Deleting this folder is always safe",
        subtype="DIR_PATH",
    )

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.prop(self, "game_path")
        row.operator("w2t.detect_game", text="", icon="VIEWZOOM")
        if self.game_path and not cooked_dir(self.game_path):
            layout.label(text="No CookedPC\\pack0.dzip found in that folder", icon="ERROR")
        layout.prop(self, "cache_path")
        layout.label(text="Cache in use: " + cache_dir(context), icon="FILE_FOLDER")
        msg = importer.w3_tools_status(context)
        layout.label(text=msg or "Witcher 3 Tools found", icon="ERROR" if msg else "CHECKMARK")


# ---------------------------------------------------------------- operators

class W2T_OT_DetectGame(bpy.types.Operator):
    """Look for The Witcher 2 in common Steam and GOG locations"""
    bl_idname = "w2t.detect_game"
    bl_label = "Find Witcher 2"

    def execute(self, context):
        found = auto_detect_game_path()
        if not found:
            self.report({"WARNING"}, "Witcher 2 not found automatically; set the folder by hand")
            return {"CANCELLED"}
        get_prefs(context).game_path = found
        self.report({"INFO"}, "Found Witcher 2 at " + found)
        return {"FINISHED"}


class W2T_OT_Refresh(bpy.types.Operator):
    """Read the game archives and list the available monsters"""
    bl_idname = "w2t.refresh"
    bl_label = "Refresh List"

    def execute(self, context):
        prefs = get_prefs(context)
        if not cooked_dir(prefs.game_path):
            found = auto_detect_game_path()
            if found:
                prefs.game_path = found
        dset = get_dset(context)
        if dset is None:
            self.report({"ERROR"}, "Set your Witcher 2 folder in the add-on preferences")
            return {"CANCELLED"}
        wm = context.window_manager
        _fill_list(wm, dset)
        self.report({"INFO"}, "%d %s listed" % (len(wm.w2t_items), wm.w2t_kind.lower()))
        return {"FINISHED"}


def _fill_list(wm, dset):
    wm.w2t_items.clear()
    for group, stem, path in importer.build_catalog(dset, wm.w2t_kind):
        it = wm.w2t_items.add()
        it.name = stem
        it.group = group
        it.path = path
    wm.w2t_index = 0


def _on_kind(self, context):
    """Switching tabs re-reads the list straight away (the archive index is cached)."""
    dset = get_dset(context)
    if dset is not None:
        _fill_list(self, dset)
    else:
        self.w2t_items.clear()


class W2T_OT_Import(bpy.types.Operator):
    """Extract, convert and import the selected entity with its textures"""
    bl_idname = "w2t.import_selected"
    bl_label = "Import Selected"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        wm = context.window_manager
        return 0 <= wm.w2t_index < len(wm.w2t_items)

    def execute(self, context):
        msg = importer.w3_tools_status(context)
        if msg:
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}
        dset = get_dset(context)
        if dset is None:
            self.report({"ERROR"}, "Set your Witcher 2 folder in the add-on preferences")
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        wm = context.window_manager
        item = wm.w2t_items[wm.w2t_index]
        try:
            root, lines = importer.import_entity(
                context, dset, item.path, cache_dir(context),
                materials=wm.w2t_materials, lods=wm.w2t_lods)
        except Exception as e:
            self.report({"ERROR"}, "Import failed: %s" % e)
            import traceback
            traceback.print_exc()
            return {"CANCELLED"}
        for line in lines:
            print("[Witcher 2 Tools] %s: %s" % (item.name, line))
        if root is not None:
            for o in context.selected_objects:
                o.select_set(False)
            root.select_set(True)
            context.view_layer.objects.active = root
        self.report({"INFO"}, "%s imported. %s" % (item.name, " | ".join(lines)))
        return {"FINISHED"}


class W2T_OT_OpenCache(bpy.types.Operator):
    """Open the cache folder in your file manager"""
    bl_idname = "w2t.open_cache"
    bl_label = "Open Cache Folder"

    def execute(self, context):
        path = cache_dir(context)
        os.makedirs(path, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return {"FINISHED"}


# ----------------------------------------------------------------------- UI

class W2T_UL_Catalog(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.label(text=item.name, icon="OUTLINER_OB_ARMATURE")
        sub = row.row()
        sub.alignment = "RIGHT"
        sub.enabled = False
        sub.label(text=item.group)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flt = [self.bitflag_filter_item] * len(items)
        if self.filter_name:
            needle = self.filter_name.lower()
            flt = [self.bitflag_filter_item if (needle in it.name.lower() or needle in it.group.lower()) else 0
                   for it in items]
        return flt, []


class W2T_PT_Main(bpy.types.Panel):
    bl_label = "Witcher 2"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Witcher 2"

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        prefs = get_prefs(context)

        msg = importer.w3_tools_status(context)
        if msg:
            box = layout.box()
            box.alert = True
            box.label(text=msg, icon="ERROR")
        if prefs and not cooked_dir(prefs.game_path):
            box = layout.box()
            box.label(text="Witcher 2 folder not set", icon="INFO")
            box.operator("w2t.detect_game", icon="VIEWZOOM")

        row = layout.row(align=True)
        row.scale_y = 1.3
        row.prop(wm, "w2t_kind", expand=True)
        layout.operator("w2t.refresh", icon="FILE_REFRESH")

        if not wm.w2t_items:
            layout.label(text="Press Refresh List to read your game files")
        layout.template_list("W2T_UL_Catalog", "", wm, "w2t_items", wm, "w2t_index", rows=12)

        col = layout.column(align=True)
        col.prop(wm, "w2t_materials")
        col.prop(wm, "w2t_lods")
        layout.operator("w2t.import_selected", icon="IMPORT")
        layout.operator("w2t.open_cache", icon="FILE_FOLDER")


# ------------------------------------------------------------- registration

CLASSES = (
    W2T_CatalogItem,
    W2T_Preferences,
    W2T_OT_DetectGame,
    W2T_OT_Refresh,
    W2T_OT_Import,
    W2T_OT_OpenCache,
    W2T_UL_Catalog,
    W2T_PT_Main,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    wm = bpy.types.WindowManager
    wm.w2t_items = CollectionProperty(type=W2T_CatalogItem)
    wm.w2t_index = IntProperty(name="Selected", default=0)
    wm.w2t_kind = EnumProperty(
        name="List",
        items=(("MONSTERS", "Monsters", "Monsters and creatures", "GHOST_ENABLED", 0),
               ("CHARACTERS", "Characters", "Named characters, crowd and soldier types (humans, elves, dwarves) and Geralt",
                "COMMUNITY", 1),
               ("OTHER", "Other", "Animals and other creatures", "OUTLINER_OB_POINTCLOUD", 2)),
        default="MONSTERS",
        update=_on_kind)
    wm.w2t_materials = BoolProperty(name="Import Textures", default=True,
                                    description="Import materials and textures")
    wm.w2t_lods = BoolProperty(name="Keep LODs", default=False,
                               description="Also import the lower-detail LOD meshes")


def unregister():
    wm = bpy.types.WindowManager
    for attr in ("w2t_items", "w2t_index", "w2t_kind", "w2t_materials", "w2t_lods"):
        if hasattr(wm, attr):
            delattr(wm, attr)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    dzip.clear_sets()
