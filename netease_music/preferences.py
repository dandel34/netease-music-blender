# -*- coding: utf-8 -*-
"""插件偏好设置（编辑 → 偏好设置 → 插件 → 网易云音乐）。"""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences

from . import player, utils

QUALITY_ITEMS = [
    (key, label, "请求该音质；拿不到时自动降级", index)
    for index, (key, label) in enumerate(utils.QUALITY_LABELS)
]

TRANSPORT_ITEMS = [
    ("auto", "自动（推荐）", "按历史成功率在 eapi / 普通 /api / weapi 之间自动切换"),
    ("eapi", "eapi（App 接口）", "走 interface.music.163.com，加密方式 AES-ECB"),
    ("plain", "普通 /api", "走 music.163.com/api，只用 Cookie，不加密"),
    ("weapi", "weapi（网页接口）", "走 music.163.com/weapi，双层 AES + RSA"),
]


class NM_Preferences(AddonPreferences):
    bl_idname = __package__

    quality: EnumProperty(name="音质", items=QUALITY_ITEMS, default="exhigh")
    preferred_transport: EnumProperty(
        name="接口协议", items=TRANSPORT_ITEMS, default="auto",
        description="如果某种协议在你的网络下不可用（例如一直返回空响应），可以手动指定另一种",
    )
    real_ip: StringProperty(
        name="RealIP", default="",
        description="部分网络环境下网易云要求带一个中国大陆 IP，例如 116.25.146.177；留空则不加",
    )
    cache_dir: StringProperty(
        name="缓存目录", subtype="DIR_PATH", default="",
        description="音频缓存位置；留空则使用系统临时目录下的 blender_netease_music",
    )
    cookie: StringProperty(name="登录 Cookie", subtype="PASSWORD", default="")
    radar_playlist_id: StringProperty(
        name="私人雷达歌单 ID", default="",
        description="自动找不到「私人雷达」时，可以在这里手动填歌单 ID（歌单链接里 id= 后面的数字）",
    )
    timeout: IntProperty(name="请求超时（秒）", default=20, min=5, max=120)
    default_volume: FloatProperty(name="默认音量", default=0.8, min=0.0, max=1.0, subtype="FACTOR")
    auto_play_next: BoolProperty(name="自动播放下一首", default=True)
    show_lyric_in_panel: BoolProperty(name="在面板里显示歌词", default=False)
    debug_requests: BoolProperty(name="打印调试日志", default=False)
    persistent_cookie: BoolProperty(
        name="记住登录状态", default=True,
        description="把 Cookie 保存在 Blender 用户偏好里，重启后免登录；关闭后退出 Blender 即失效",
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True

        box = layout.box()
        box.label(text="播放", icon="PLAY")
        box.prop(self, "quality")
        box.prop(self, "default_volume")
        box.prop(self, "auto_play_next")
        row = box.row()
        row.prop(self, "cache_dir")
        count, size = player.cache_size(self.cache_dir or utils.default_cache_dir())
        row = box.row()
        row.alignment = "RIGHT"
        row.label(text="缓存：%d 个文件 / %s" % (count, player.human_size(size)))
        row.operator("netease.clear_cache", text="清空缓存", icon="TRASH")

        box = layout.box()
        box.label(text="网络", icon="URL")
        box.prop(self, "preferred_transport")
        box.prop(self, "real_ip")
        box.prop(self, "timeout")
        box.prop(self, "debug_requests")
        box.operator("netease.self_test", icon="CHECKMARK")

        box = layout.box()
        box.label(text="账号", icon="USER")
        box.prop(self, "persistent_cookie")
        box.prop(self, "cookie")
        box.operator("netease.logout", icon="TRASH")
        box.prop(self, "radar_playlist_id")
        box.prop(self, "show_lyric_in_panel")

        if not player.aud_available():
            warn = layout.box()
            warn.label(text="audio 模块不可用", icon="ERROR")
            warn.label(text=player.aud_error())


CLASSES = (NM_Preferences,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # noqa: BLE001
            pass
