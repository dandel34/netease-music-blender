# -*- coding: utf-8 -*-
"""校验提交的安装包与源码一致（不需要 Blender，CI 里跑）。

能拦住这几类问题：

* 改了源码却忘记重新打包 → zip 里的文件和源码不一致；
* manifest 版本没跟上 ``bl_info``；
* zip 里混进 ``__pycache__`` 或多余文件；
* manifest 缺字段 / id 与包目录名不一致。

    python tools/check_package.py
"""

import ast
import pathlib
import sys
import tomllib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "netease_music"
DIST = ROOT / "dist"
SKIP_SUFFIX = {".pyc", ".pyo"}
SKIP_DIRS = {"__pycache__", ".mypy_cache"}
REQUIRED_KEYS = ("schema_version", "id", "version", "name", "tagline",
                 "maintainer", "type", "blender_version_min", "license")

FAILURES = []


def check(name, ok, detail=""):
    print("  [%s] %s%s" % ("通过" if ok else "失败", name, "" if ok else "  " + str(detail)))
    if not ok:
        FAILURES.append(name)


def addon_version():
    tree = ast.parse((PKG_DIR / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "bl_info":
            return ".".join(str(part) for part in ast.literal_eval(node.value)["version"])
    raise SystemExit("没有在 %s 里找到 bl_info" % (PKG_DIR / "__init__.py"))


def source_files():
    for path in sorted(PKG_DIR.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix in SKIP_SUFFIX or any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def main():
    version = addon_version()
    manifest_path = PKG_DIR / "blender_manifest.toml"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    try:
        manifest = tomllib.loads(manifest_text)
    except Exception as exc:  # noqa: BLE001
        check("blender_manifest.toml 能被解析", False, exc)
        manifest = {}

    print("== manifest ==")
    check("必备字段齐全", all(key in manifest for key in REQUIRED_KEYS),
          [key for key in REQUIRED_KEYS if key not in manifest])
    check("版本与 bl_info 一致", manifest.get("version") == version,
          "manifest=%s bl_info=%s" % (manifest.get("version"), version))
    check("id 与包目录名一致", manifest.get("id") == PKG_DIR.name,
          "id=%s 目录=%s" % (manifest.get("id"), PKG_DIR.name))
    check("type 是 add-on", manifest.get("type") == "add-on", manifest.get("type"))

    print("== 安装包 ==")
    zip_path = DIST / ("netease_music-%s.zip" % version)
    if not zip_path.is_file():
        check("安装包存在", False, zip_path.name)
        return finish()
    check("安装包存在", True, zip_path.name)

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        expected = {"%s/%s" % (PKG_DIR.name, path.relative_to(PKG_DIR).as_posix())
                    for path in source_files()}
        # 生成 manifest 也应在包里（内容必须与源码一致）
        expected.add("%s/blender_manifest.toml" % PKG_DIR.name)

        missing = sorted(expected - names)
        extra = sorted(names - expected)
        check("源码里的文件都在包里", not missing, missing)
        check("包里没有多余文件", not extra, extra)
        check("包里没有 __pycache__", not any("__pycache__" in name for name in names),
              [n for n in names if "__pycache__" in n])

        mismatched = []
        for path in source_files():
            arcname = "%s/%s" % (PKG_DIR.name, path.relative_to(PKG_DIR).as_posix())
            if arcname in names and zf.read(arcname) != path.read_bytes():
                mismatched.append(arcname)
        check("每个文件的内容都与源码一致（改完记得重新打包）", not mismatched, mismatched)

        zip_manifest = "%s/blender_manifest.toml" % PKG_DIR.name
        if zip_manifest in names:
            in_zip = zf.read(zip_manifest).decode("utf-8")
            check("包里的 manifest 与源码一致", in_zip == manifest_text,
                  "字节数 %d vs %d" % (len(in_zip), len(manifest_text)))
        else:
            check("包里有 manifest", False, zip_manifest)

    return finish()


def finish():
    print()
    if FAILURES:
        print("失败项：%s" % "、".join(FAILURES))
        return 1
    print("安装包与源码一致（全部检查通过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
