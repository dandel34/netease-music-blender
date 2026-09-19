# -*- coding: utf-8 -*-
"""网易云音乐 · Blender 内置音乐播放插件。

在 Blender 里直接登录网易云账号、浏览自己的歌单（含「我喜欢的音乐」）、
查看「每日推荐」与「私人雷达」，并用 Blender 自带的 aud 模块在内部播放，
不需要任何外部播放器。

面板位置：3D 视图 → 侧栏（快捷键 N）→「网易云音乐」。

说明：只有真正需要 Blender 的模块（props / ops / ui / runtime / preferences）
才在 register() 里延迟导入，这样 crypto / api / utils 这些纯逻辑模块可以在
Blender 之外直接 import 做单元测试。
"""

bl_info = {
    "name": "NetEase Cloud Music（网易云音乐）",
    "author": "DSH",
    "version": (1, 2, 0),
    "blender": (3, 6, 0),
    "location": "3D 视图 → 侧栏 N → 网易云音乐",
    "description": "在 Blender 内登录网易云音乐，浏览歌单 / 每日推荐 / 私人雷达，播放并显示可拖动的动态歌词",
    "category": "Audio",
}

__version__ = ".".join(str(part) for part in bl_info["version"])

#: 主循环定时器状态
_TIMER = {"registered": False}


def _log(*args):
    print("[网易云音乐]", *args)


def _tick():
    """主线程定时器：回收后台任务结果、刷新播放进度、轮询扫码状态。"""
    from . import runtime
    return runtime.tick()


def _start_timer():
    import bpy
    if _TIMER["registered"]:
        return
    try:
        if not bpy.app.timers.is_registered(_tick):
            bpy.app.timers.register(_tick, first_interval=0.5, persistent=True)
        _TIMER["registered"] = True
    except Exception as exc:  # noqa: BLE001
        _log("启动定时器失败：%s" % exc)


def _stop_timer():
    import bpy
    try:
        if bpy.app.timers.is_registered(_tick):
            bpy.app.timers.unregister(_tick)
    except Exception:  # noqa: BLE001
        pass
    _TIMER["registered"] = False


def register():
    import bpy
    from . import ops, overlay, preferences, props, runtime, ui, utils

    props.register()
    preferences.register()
    ops.register()
    ui.register()
    # 动态歌词浮层：绘制回调 + 快捷键
    overlay.register_handler()
    try:
        settings = bpy.context.preferences.addons[__package__].preferences
        overlay.refresh_keymap(settings.lyric_hotkey)
    except Exception:  # noqa: BLE001
        overlay.register_keymap()
    _start_timer()
    try:
        settings = bpy.context.preferences.addons[__package__].preferences
        runtime.engine().set_volume(settings.default_volume)
        utils.debug_mode(settings.debug_requests)
        runtime.apply_audio_settings(bpy.context)      # 音频后端 / 缓冲 / 内存缓存
    except Exception:  # noqa: BLE001
        pass
    _log("已启用（版本 %s）" % __version__)


def unregister():
    import bpy
    from . import jobs, ops, overlay, preferences, props, runtime, ui

    _stop_timer()
    jobs.cancel_all()
    overlay.unregister_keymap()
    overlay.unregister_handler()
    try:
        runtime.engine().stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        settings = bpy.context.preferences.addons[__package__].preferences
        if not settings.persistent_cookie:
            settings.cookie = ""
    except Exception:  # noqa: BLE001
        pass
    ui.unregister()
    ops.unregister()
    preferences.unregister()
    props.unregister()
    _log("已停用")
