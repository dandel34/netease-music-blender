# -*- coding: utf-8 -*-
"""验证「从磁盘安装」流程：把 dist 里的 zip 装进临时的用户目录并启用。

    blender --background --factory-startup --python tools/test_install_in_blender.py

调用时请把 BLENDER_USER_SCRIPTS / BLENDER_USER_CONFIG 指到临时目录，
这样不会动到你真正的 Blender 配置。
"""

import json
import os
import sys
import traceback

import bpy

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
TMP = os.path.join(ROOT, ".tmp")
sys.path.insert(0, ROOT)
REPORT = {"blender": bpy.app.version_string, "user_scripts": os.environ.get("BLENDER_USER_SCRIPTS", "")}


def main():
    import glob
    zips = sorted(glob.glob(os.path.join(ROOT, "dist", "netease_music-*.zip")))
    REPORT["zip"] = os.path.basename(zips[-1]) if zips else None
    REPORT["extension_ops"] = sorted(o for o in dir(bpy.ops.extensions) if "install" in o or "validate" in o)
    if not zips:
        REPORT["error"] = "没有找到 dist 里的 zip"
        return
    zip_path = zips[-1]

    try:
        result = bpy.ops.preferences.addon_install(filepath=zip_path, overwrite=True)
        REPORT["addon_install"] = list(result)
    except Exception as exc:  # noqa: BLE001
        REPORT["addon_install"] = "%s: %s" % (type(exc).__name__, exc)
        REPORT["addon_install_tb"] = traceback.format_exc()[-500:]

    candidates = ["netease_music"]
    try:
        for repo in bpy.context.preferences.extensions.repos:
            candidates.append("bl_ext.%s.netease_music" % repo.module)
    except Exception:  # noqa: BLE001
        pass
    REPORT["candidates"] = candidates

    enabled = None
    for module in candidates:
        try:
            bpy.ops.preferences.addon_enable(module=module)
            enabled = module
            break
        except Exception as exc:  # noqa: BLE001
            REPORT.setdefault("enable_errors", {})[module] = "%s: %s" % (type(exc).__name__, exc)
    REPORT["enabled_as"] = enabled

    REPORT["panel_registered"] = hasattr(bpy.types, "NM_PT_main")
    REPORT["state_registered"] = hasattr(bpy.types.Scene, "netease")
    REPORT["operators"] = len([n for n in dir(bpy.ops.netease) if not n.startswith("_")])
    try:
        addons = [name for name in bpy.context.preferences.addons.keys() if "netease" in name]
        REPORT["prefs_addons"] = addons
    except Exception:  # noqa: BLE001
        pass
    # 面板能查到分类名（说明真的注册上了）
    try:
        REPORT["panel_category"] = bpy.types.NM_PT_main.bl_category
        REPORT["panel_label"] = bpy.types.NM_PT_main.bl_label
    except Exception as exc:  # noqa: BLE001
        REPORT["panel_info_error"] = str(exc)

    out = os.path.join(TMP, "install_report.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(REPORT, fh, ensure_ascii=False, indent=1)
    print(json.dumps(REPORT, ensure_ascii=False, indent=1))


main()
