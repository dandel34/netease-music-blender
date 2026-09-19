# -*- coding: utf-8 -*-
"""动态歌词浮层：在 3D 视图里画一个可拖动、可开关的同步歌词面板。

绘制方式是 Blender 官方的做法：把回调挂在
``SpaceView3D.draw_handler_add(..., 'WINDOW', 'POST_PIXEL')`` 上，
用 ``gpu`` 画底板/进度条、用 ``blf`` 画文字。

实测结论（Blender 5.2，写代码前逐条验证过）：

* 加载字体是 ``blf.load(path)``（老教程里的 ``blf.font_load`` 已经不存在）；
* ``blf.size(fontid, size)`` 是 2 个参数，``blf.color(fontid, r, g, b, a)`` 是 5 个参数；
* **默认字体（fontid 0）自带中文回退**：``blf.dimensions(0, "中文歌词测试")`` 返回
  132×22（6 个字 × 22px），所以中文歌词不需要额外装字体；
* 后台模式（``--background``）没有 GPU 上下文，``blf.draw`` 会直接让 Blender 崩溃、
  ``gpu.*`` 会抛 ``SystemError``。所以绘制回调整体包在 try/except 里，
  并且**所有绘制调用都只在这个回调内部发生**（模块导入期不碰 GPU）。

布局计算拆成了纯函数 :func:`build_scene`，不依赖 GPU，便于在无界面环境下单测。
"""

from __future__ import annotations

import bpy

from . import lyrics as lyrics_mod, runtime, utils

try:  # pragma: no cover - 取决于 Blender 构建
    import blf
    import gpu
    from gpu_extras.batch import batch_for_shader
except Exception:  # noqa: BLE001
    blf = None
    gpu = None
    batch_for_shader = None

#: 品牌红（网易云 #C20C0C）
ACCENT = (0.76, 0.05, 0.05, 1.0)

_HANDLE = None
_SHADER = None
_FONTS = {}                      # 路径 → fontid
_ERROR = {"message": ""}


# --------------------------------------------------------------------------
# 布局计算（纯函数，可单测）
# --------------------------------------------------------------------------


def _text_width(text: str, size: int, measure=None) -> float:
    """文字宽度：有测量函数就用它，否则按中英文粗估，保证布局逻辑可离线测试。"""
    if measure is not None:
        try:
            return float(measure(text, size))
        except Exception:  # noqa: BLE001
            pass
    width = 0.0
    for char in text:
        width += size * (1.0 if ord(char) > 0x2E80 else 0.56)
    return width


def _trim_lines(items, max_lines: int) -> list:
    """行数放不下时，优先丢“当前行之前”的历史行，再丢末尾的后续行。"""
    if not items or max_lines <= 0:
        return []
    if len(items) <= max_lines:
        return list(items)
    current = next((index for index, (offset, _item) in enumerate(items) if offset == 0), 0)
    drop = len(items) - max_lines
    start = min(drop, current)
    trimmed = list(items[start:])
    if len(trimmed) > max_lines:
        trimmed = trimmed[:max_lines]
    return trimmed


def line_counts(settings) -> tuple:
    """当前设置下要取「当前行之上/之下」各几行（偏好里是 1/3/5/7 行）。"""
    raw = getattr(settings, "lyric_line_count", "1") if settings is not None else "1"
    return lyrics_mod.counts_for(raw)


def _layout(width, height, st, settings, window, font_size: int, measure=None) -> dict:
    others_size = max(9, int(font_size * 0.72))
    header_size = max(8, int(font_size * 0.60))
    translation_size = max(8, int(font_size * 0.66))
    padding = max(8.0, font_size * 0.55)
    spacing = font_size * 1.62
    progress_h = max(2.0, font_size * 0.12)

    show_title = bool(getattr(settings, "lyric_show_title", True))
    show_translation = bool(getattr(settings, "lyric_show_translation", True))
    opacity = float(getattr(settings, "lyric_bg_opacity", 0.55) or 0.0)

    items = list((window or {}).get("items") or [])
    has_lyric = bool(items)

    title_text = ""
    if getattr(st, "current_name", ""):
        title_text = st.current_name
        if getattr(st, "current_artists", ""):
            title_text += " - " + st.current_artists

    # 纯音乐 / 无歌词：整块只显示歌名与歌手（不再塞“暂无歌词”占位，
    # 也不受“显示歌名与歌手”开关影响——否则浮层就空了）
    if not has_lyric:
        title_is_main = True
        if not title_text:
            title_text = "♪ 未在播放 ♪"
    else:
        title_is_main = False
        if not show_title:
            title_text = ""

    translation = ""
    if has_lyric and show_translation:
        for offset, item in items:
            if offset == 0:
                translation = item.get("tr") or ""
                break

    title_size = font_size if title_is_main else header_size
    title_h = title_size * 1.9 if title_text else 0.0
    translation_h = translation_size * 1.5 if translation else 0.0

    # 视口矮的时候先减行数，保证底板不出界
    if has_lyric:
        available = max(spacing, height - 12.0 - padding * 2.0 - progress_h - 6.0 - title_h - translation_h)
        visible = _trim_lines(items, max(1, int(available / spacing)))
    else:
        visible = []

    box_w = min(max(280.0, font_size * 22.0), max(120.0, width - 24.0), 760.0)
    box_h = padding * 2.0 + len(visible) * spacing + progress_h + 6.0 + title_h + translation_h

    center_x = float(getattr(st, "lyric_pos_x", 0.5)) * width
    center_y = float(getattr(st, "lyric_pos_y", 0.2)) * height
    box_x = min(max(center_x - box_w / 2.0, 6.0), max(6.0, width - box_w - 6.0))
    box_y = min(max(center_y - box_h / 2.0, 6.0), max(6.0, height - box_h - 6.0))

    rects = []
    if opacity > 0.001:
        rects.append({"x": box_x, "y": box_y, "w": box_w, "h": box_h,
                      "color": (0.05, 0.05, 0.07, min(1.0, opacity))})
    if getattr(st, "lyric_drag_active", False):
        edge = 2.0
        for rx, ry, rw, rh in ((box_x, box_y, box_w, edge), (box_x, box_y + box_h - edge, box_w, edge),
                               (box_x, box_y, edge, box_h), (box_x + box_w - edge, box_y, edge, box_h)):
            rects.append({"x": rx, "y": ry, "w": rw, "h": rh, "color": ACCENT})

    texts = []
    cursor_y = box_y + box_h - padding
    if title_text:
        cursor_y -= title_size * 1.3
        color = (1.0, 0.94, 0.94, 0.96) if title_is_main else (0.86, 0.86, 0.90, 0.85)
        texts.append({"text": title_text, "size": title_size, "center_y": cursor_y,
                      "color": color, "center_x": box_x + box_w / 2.0, "title": True})

    for offset, item in visible:
        cursor_y -= spacing
        is_current = offset == 0
        size = font_size if is_current else others_size
        color = (1.0, 1.0, 1.0, 1.0) if is_current else (0.78, 0.78, 0.84, 0.55)
        texts.append({"text": item.get("text") or "", "size": size, "center_y": cursor_y,
                      "color": color, "center_x": box_x + box_w / 2.0, "current": is_current})
        if is_current and translation:
            cursor_y -= translation_size * 1.45
            texts.append({"text": translation, "size": translation_size, "center_y": cursor_y,
                          "color": (0.72, 0.82, 0.98, 0.85), "center_x": box_x + box_w / 2.0})

    try:
        ratio = max(0.0, min(1.0, float(st.progress_ratio)))
    except Exception:  # noqa: BLE001
        ratio = 0.0
    bar_y = box_y + padding * 0.45
    bar_w = box_w - padding * 2.0
    rects.append({"x": box_x + padding, "y": bar_y, "w": bar_w, "h": progress_h,
                  "color": (1.0, 1.0, 1.0, 0.16)})
    if ratio > 0.001:
        rects.append({"x": box_x + padding, "y": bar_y, "w": bar_w * ratio, "h": progress_h,
                      "color": ACCENT})

    return {"rects": rects, "texts": texts, "box": (box_x, box_y, box_w, box_h),
            "lines": len(visible), "font_size": font_size, "has_lyric": has_lyric}


def build_scene(width: int, height: int, st, settings, window, measure=None) -> dict:
    """算出这一帧要画的矩形与文字（不碰 GPU，因此可以无界面测试）。

    先用偏好里的字号算一遍；如果视口太矮导致底板超出区域，就缩小字号重算一次。
    返回 ``{"rects": [...], "texts": [...], "box": (x, y, w, h), "lines": n}``。
    """
    width = max(120, int(width))
    height = max(80, int(height))
    font_size = int(getattr(settings, "lyric_font_size", 26) or 26)
    scene = _layout(width, height, st, settings, window, font_size, measure)
    overflow = scene["box"][3] - (height - 12.0)
    if overflow > 0 and font_size > 10:
        ratio = max(0.25, (height - 12.0) / scene["box"][3])
        smaller = max(9, int(font_size * ratio))
        if smaller < font_size:
            scene = _layout(width, height, st, settings, window, smaller, measure)
    return scene


def clamp_position(value: float, low: float = 0.02, high: float = 0.98) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# GPU 绘制
# --------------------------------------------------------------------------


def _get_shader():
    global _SHADER
    if _SHADER is None:
        _SHADER = gpu.shader.from_builtin("UNIFORM_COLOR")
    return _SHADER


def _draw_rect(shader, x, y, w, h, color):
    if w <= 0 or h <= 0:
        return
    batch = batch_for_shader(shader, "TRI_FAN",
                             {"pos": [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _font_id(settings) -> int:
    """返回要用的字体 id；默认 0（自带中文回退），也可用偏好里指定的字体。"""
    path = (getattr(settings, "lyric_font_path", "") or "").strip()
    if not path:
        return 0
    path = bpy.path.abspath(path)
    if path in _FONTS:
        return _FONTS[path]
    try:
        font_id = blf.load(path)
    except Exception as exc:  # noqa: BLE001
        utils.log("加载歌词字体失败（%s）：%s" % (path, exc))
        font_id = 0
    _FONTS[path] = font_id
    return font_id


def draw():
    """draw handler 回调：每帧在 3D 视图上叠一层歌词。"""
    global _ERROR
    try:
        context = bpy.context
        scene = getattr(context, "scene", None)
        st = getattr(scene, "netease", None) if scene else None
        if st is None or not st.lyric_overlay:
            return
        region = getattr(context, "region", None)
        if region is None or blf is None or gpu is None:
            return
        settings = runtime.prefs(context)
        font_id = _font_id(settings) if settings else 0

        def measure(text, size):
            blf.size(font_id, int(size))
            return blf.dimensions(font_id, text)[0]

        window = runtime.lyric_window(st, *line_counts(settings))
        scene_data = build_scene(region.width, region.height, st, settings, window, measure=measure)

        shader = _get_shader()
        gpu.state.blend_set("ALPHA")
        try:
            for rect in scene_data["rects"]:
                _draw_rect(shader, rect["x"], rect["y"], rect["w"], rect["h"], rect["color"])
            blf.enable(font_id, blf.SHADOW)
            blf.shadow(font_id, 3, 0.0, 0.0, 0.0, 0.9)
            for item in scene_data["texts"]:
                text = item["text"]
                if not text:
                    continue
                size = int(item["size"])
                blf.size(font_id, size)
                blf.color(font_id, *item["color"])
                width = _text_width(text, size, measure)
                x = item["center_x"] - width / 2.0
                # blf 以基线定位，往上抬约 0.35 个字高看起来才是垂直居中
                blf.position(font_id, x, item["center_y"] - size * 0.35, 0)
                blf.draw(font_id, text)
            blf.disable(font_id, blf.SHADOW)
        finally:
            try:
                gpu.state.blend_set("NONE")
            except Exception:  # noqa: BLE001
                pass
        if _ERROR["message"]:
            _ERROR["message"] = ""
    except Exception as exc:  # noqa: BLE001 - 绘制出错绝不能影响 Blender 渲染
        message = "%s: %s" % (type(exc).__name__, exc)
        if _ERROR["message"] != message:
            _ERROR["message"] = message
            utils.log("歌词浮层绘制失败：%s" % message)


def draw_error() -> str:
    return _ERROR["message"]


# --------------------------------------------------------------------------
# 注册 / 注销
# --------------------------------------------------------------------------


def register_handler():
    global _HANDLE
    if _HANDLE is not None:
        return
    try:
        _HANDLE = bpy.types.SpaceView3D.draw_handler_add(draw, (), "WINDOW", "POST_PIXEL")
    except Exception as exc:  # noqa: BLE001
        _HANDLE = None
        utils.log("注册歌词浮层失败：%s" % exc)


def unregister_handler():
    global _HANDLE
    if _HANDLE is None:
        return
    try:
        bpy.types.SpaceView3D.draw_handler_remove(_HANDLE, "WINDOW")
    except Exception:  # noqa: BLE001
        pass
    _HANDLE = None


def is_registered() -> bool:
    return _HANDLE is not None


# --------------------------------------------------------------------------
# 快捷键（Ctrl+Alt+L 开关浮层）
# --------------------------------------------------------------------------

_KEYMAPS = []


def register_keymap():
    if _KEYMAPS:
        return
    try:
        keyconfig = bpy.context.window_manager.keyconfigs.addon
    except Exception:  # noqa: BLE001
        keyconfig = None
    if keyconfig is None:            # 后台模式没有 addon 键位配置
        return
    try:
        keymap = keyconfig.keymaps.new(name="3D View", space_type="VIEW_3D")
        item = keymap.keymap_items.new("netease.toggle_lyric_overlay", "L", "PRESS", ctrl=True, alt=True)
        try:
            item.properties.mode = "toggle"
        except Exception:  # noqa: BLE001
            pass
        _KEYMAPS.append((keymap, item))
    except Exception as exc:  # noqa: BLE001
        utils.log("注册歌词快捷键失败：%s" % exc)


def unregister_keymap():
    while _KEYMAPS:
        keymap, item = _KEYMAPS.pop()
        try:
            keymap.keymap_items.remove(item)
        except Exception:  # noqa: BLE001
            pass


def refresh_keymap(enabled: bool):
    if enabled:
        register_keymap()
    else:
        unregister_keymap()


def keymap_registered() -> bool:
    return bool(_KEYMAPS)
