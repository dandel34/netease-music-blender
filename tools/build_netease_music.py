# -*- coding: utf-8 -*-
"""打包网易云音乐插件为可安装的扩展 zip。

输出：

* ``dist/netease_music/``            —— 扩展包源码（多文件包）
* ``dist/netease_music-<版本>.zip``  —— 「从磁盘安装」用的安装包（Blender 4.2+ 扩展格式）

版本号取自 ``netease_music/__init__.py`` 的 ``bl_info``，manifest 由本脚本生成，
不会出现两处版本不一致。

    python tools/build_netease_music.py
"""

import ast
import pathlib
import shutil
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "netease_music"
DIST = ROOT / "dist"
PKG = DIST / "netease_music"

SKIP_SUFFIX = {".pyc", ".pyo"}
SKIP_DIRS = {"__pycache__", ".mypy_cache"}


def addon_meta():
    tree = ast.parse((SOURCE / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "bl_info":
            info = ast.literal_eval(node.value)
            return ".".join(str(part) for part in info["version"]), info
    raise SystemExit("没有在 %s 里找到 bl_info" % SOURCE)


VERSION, BL_INFO = addon_meta()

MANIFEST = '''schema_version = "1.0.0"

id = "netease_music"
version = "{version}"
name = "NetEase Cloud Music"
tagline = "在 Blender 内登录网易云音乐，浏览歌单 / 每日推荐 / 私人雷达并直接播放"
maintainer = "{author}"
type = "add-on"

blender_version_min = "4.2.0"
license = ["SPDX:GPL-3.0-or-later"]

tags = ["Sequencer", "User Interface"]
'''.format(version=VERSION, author=BL_INFO["author"])


def copy_sources():
    if PKG.exists():
        shutil.rmtree(PKG)
    PKG.mkdir(parents=True)
    for path in sorted(SOURCE.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix in SKIP_SUFFIX or any(part in SKIP_DIRS for part in path.parts):
            continue
        target = PKG / path.relative_to(SOURCE)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    # 固定用 LF 写出，避免 Windows 上生成 CRLF 让产物与源码不一致
    with open(PKG / "blender_manifest.toml", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(MANIFEST)


def build_zip():
    """打包；条目时间戳固定，同样内容产出同样的字节，便于 CI/代码评审比较。"""
    zip_path = DIST / ("netease_music-%s.zip" % VERSION)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(PKG.rglob("*")):
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(path.relative_to(DIST).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    return zip_path


def main():
    DIST.mkdir(exist_ok=True)
    copy_sources()
    zip_path = build_zip()
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        print("zip 内容：")
        for info in zf.infolist():
            total += info.file_size
            print("  %-42s %7d 字节" % (info.filename, info.file_size))
    print("未压缩合计 %d 字节，压缩包 %d 字节 → %s" % (total, zip_path.stat().st_size, zip_path))


if __name__ == "__main__":
    main()
