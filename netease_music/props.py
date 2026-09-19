# -*- coding: utf-8 -*-
"""插件的 RNA 属性：面板、列表和播放状态全靠它们驱动。

注意：网易云的歌曲 ID 超过 32 位整数范围，所以 ID 一律用字符串保存，
避免在 Blender 的 IntProperty 上溢出。
"""

from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import PropertyGroup

from . import utils

#: 滑动条提交的跳转请求（由定时器消费，避免与播放进度互相打架）
PENDING_SEEK = {"value": None}
#: 定时器回写进度时置位，防止把“同步显示”误当成“用户拖动”
_SYNCING = {"on": False}


def request_seek(value: float):
    PENDING_SEEK["value"] = float(value)


def take_seek():
    value = PENDING_SEEK["value"]
    PENDING_SEEK["value"] = None
    return value


def sync_seek(st, value: float):
    """由定时器调用：把播放进度写回滑动条而不触发跳转。"""
    _SYNCING["on"] = True
    try:
        st.seek_seconds = float(value)
    finally:
        _SYNCING["on"] = False


def _seek_updated(self, context):
    if _SYNCING["on"]:
        return
    request_seek(self.seek_seconds)


class NM_Track(PropertyGroup):
    """列表里的一首歌。"""

    song_id: StringProperty(name="ID")
    name: StringProperty(name="歌名")
    artists: StringProperty(name="歌手")
    album: StringProperty(name="专辑")
    duration: IntProperty(name="时长（毫秒）")
    duration_text: StringProperty(name="时长")
    fee: IntProperty(name="付费类型")
    mv_id: StringProperty(name="MV")
    url: StringProperty(name="播放地址")
    liked: BoolProperty(name="已加入我喜欢的音乐")

    def fill(self, track: dict, url: str = ""):
        self.song_id = str(track.get("id") or "")
        self.name = track.get("name") or "未知歌曲"
        self.artists = track.get("artists") or "未知歌手"
        self.album = track.get("album") or ""
        self.duration = int(track.get("duration") or 0)
        self.duration_text = utils.format_duration(self.duration)
        self.fee = int(track.get("fee") or 0)
        self.mv_id = str(track.get("mv_id") or 0)
        self.url = url or ""

    def as_dict(self) -> dict:
        return {
            "id": self.song_id,
            "name": self.name,
            "artists": self.artists,
            "album": self.album,
            "duration": self.duration,
            "fee": self.fee,
            "mv_id": self.mv_id,
        }


class NM_Playlist(PropertyGroup):
    """我的歌单 / 收藏歌单里的一项。"""

    playlist_id: StringProperty(name="ID")
    name: StringProperty(name="名称")
    track_count: IntProperty(name="曲目数")
    creator: StringProperty(name="创建者")
    special_type: IntProperty(name="特殊类型")
    subscribed: BoolProperty(name="收藏")
    description: StringProperty(name="简介")

    def fill(self, playlist: dict):
        self.playlist_id = str(playlist.get("id") or "")
        self.name = playlist.get("name") or "未命名歌单"
        self.track_count = int(playlist.get("track_count") or 0)
        self.creator = playlist.get("creator") or ""
        self.special_type = int(playlist.get("special_type") or 0)
        self.subscribed = bool(playlist.get("subscribed"))
        self.description = playlist.get("description") or ""


def _tag_redraw(self=None, context=None):
    """属性变化后请求重画面板。"""
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
    except Exception:  # noqa: BLE001
        pass


class NM_State(PropertyGroup):
    """挂在场景上的全部插件状态。"""

    # ------------------------------------------------------------ 界面
    tab: EnumProperty(
        name="分类",
        items=[
            ("account", "账号", "登录网易云账号", "USER", 0),
            ("player", "播放", "播放控制与播放队列", "PLAY", 1),
            ("playlists", "歌单", "我的歌单与「我喜欢的音乐」", "FILE_FOLDER", 2),
            ("recommend", "推荐", "每日推荐与私人雷达", "LIGHT_SUN", 3),
        ],
        default="account",
    )
    # ------------------------------------------------------------ 账号
    logged_in: BoolProperty(name="已登录")
    nickname: StringProperty(name="昵称")
    user_id: StringProperty(name="用户 ID")
    vip_text: StringProperty(name="会员状态")
    account_message: StringProperty(name="账号提示")
    cookie_input: StringProperty(name="Cookie", subtype="PASSWORD", options={"SKIP_SAVE"})
    manual_radar_id: StringProperty(name="私人雷达歌单 ID")

    # ------------------------------------------------------------ 我喜欢的音乐
    current_liked: BoolProperty(name="当前歌曲已喜欢")
    liked_loaded: BoolProperty(name="已同步喜欢列表")
    liked_count: IntProperty(name="喜欢的歌曲数")
    liked_text: StringProperty(name="喜欢列表状态", default="尚未同步")

    # ------------------------------------------------------------ 列表
    playlists: CollectionProperty(type=NM_Playlist)
    playlist_index: IntProperty(default=0)
    tracks: CollectionProperty(type=NM_Track)
    track_index: IntProperty(default=0)
    queue: CollectionProperty(type=NM_Track)
    queue_index: IntProperty(default=0)
    list_title: StringProperty(name="当前列表", default="（还没有加载任何列表）")
    list_kind: StringProperty(name="列表来源", default="none")
    list_note: StringProperty(name="列表说明")

    # ------------------------------------------------------------ 播放
    current_name: StringProperty(name="当前歌曲", default="")
    current_artists: StringProperty(name="当前歌手", default="")
    current_album: StringProperty(name="当前专辑", default="")
    current_id: StringProperty(name="当前歌曲 ID", default="")
    current_source: StringProperty(name="音质/来源", default="")
    is_playing: BoolProperty(name="正在播放")
    is_paused: BoolProperty(name="已暂停")
    position: FloatProperty(name="播放进度（秒）")
    duration: FloatProperty(name="总时长（秒）")
    seek_seconds: FloatProperty(
        name="跳转到（秒）", default=0.0, min=0.0, soft_max=3600.0,
        description="拖动后立即跳转到该位置",
        update=_seek_updated,
    )
    volume: FloatProperty(name="音量", default=0.8, min=0.0, max=1.0, subtype="FACTOR")
    loop_mode: EnumProperty(
        name="循环",
        items=[
            ("off", "不循环", "播完当前歌曲后播放下一首"),
            ("one", "单曲循环", "一直重复当前歌曲"),
            ("all", "列表循环", "播完最后一首回到第一首"),
        ],
        default="off",
    )
    status_text: StringProperty(name="状态", default="就绪")
    error_text: StringProperty(name="错误", default="")
    downloading: BoolProperty(name="下载中")
    download_progress: FloatProperty(name="下载进度", default=0.0, subtype="FACTOR")
    download_text: StringProperty(name="下载说明", default="")

    # ------------------------------------------------------------ 其它
    lyric: StringProperty(name="歌词", default="", options={"SKIP_SAVE"})
    show_lyric: BoolProperty(name="显示歌词", default=False)
    radar_source: StringProperty(name="雷达来源", default="")
    cache_text: StringProperty(name="缓存", default="")
    self_test_report: StringProperty(name="自检报告", default="", options={"SKIP_SAVE"})

    # ------------------------------------------------------------ 便捷方法

    def tracks_clear(self):
        self.tracks.clear()
        self.track_index = 0

    def tracks_add(self, track: dict):
        item = self.tracks.add()
        item.fill(track)
        return item

    def tracks_add_full(self, track: dict, url: str = ""):
        item = self.tracks.add()
        item.fill(track, url)
        return item

    def queue_clear(self):
        self.queue.clear()
        self.queue_index = 0

    def queue_add(self, track: dict):
        item = self.queue.add()
        item.fill(track)
        return item

    def playlists_clear(self):
        self.playlists.clear()
        self.playlist_index = 0

    @property
    def progress_ratio(self) -> float:
        if self.duration <= 0.01:
            return 0.0
        return max(0.0, min(1.0, self.position / self.duration))

    def set_error(self, message: str):
        self.error_text = str(message)[:500]
        self.status_text = "出错了"

    def set_status(self, message: str):
        self.status_text = str(message)[:200]

    def clear_error(self):
        self.error_text = ""


CLASSES = (NM_Track, NM_Playlist, NM_State)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.netease = PointerProperty(type=NM_State, name="网易云音乐")


def unregister():
    if hasattr(bpy.types.Scene, "netease"):
        del bpy.types.Scene.netease
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # noqa: BLE001
            pass
