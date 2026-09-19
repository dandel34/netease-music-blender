# -*- coding: utf-8 -*-
"""操作符层：把面板上的按钮接到 runtime / api / jobs 上。

所有网络操作都是异步的：操作符本身立刻返回，结果由主线程定时器回收后写进 RNA，
因此界面不会因为一次请求卡住。
"""

from __future__ import annotations

import os

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import Operator

from . import api, crypto, jobs, overlay, player, props, runtime, utils


def _ok(op, message):
    op.report({"INFO"}, message)
    return {"FINISHED"}


def _fail(op, message):
    op.report({"ERROR"}, str(message)[:400])
    return {"CANCELLED"}


class NM_Base:
    @staticmethod
    def st(context):
        return runtime.state(context)

    @staticmethod
    def prefs(context):
        return runtime.prefs(context)


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------


class NM_OT_refresh_account(NM_Base, Operator):
    bl_idname = "netease.refresh_account"
    bl_label = "刷新登录状态"
    bl_description = "用当前 Cookie 查询账号信息"

    def execute(self, context):
        runtime.verify_login(context)
        return _ok(self, "正在校验登录状态…")


class NM_OT_paste_cookie(NM_Base, Operator):
    bl_idname = "netease.paste_cookie"
    bl_label = "从剪贴板粘贴"
    bl_description = "把剪贴板里的 Cookie 粘到输入框"

    def execute(self, context):
        st = self.st(context)
        try:
            st.cookie_input = context.window_manager.clipboard or ""
        except Exception as exc:  # noqa: BLE001
            return _fail(self, "读取剪贴板失败：%s" % exc)
        if not st.cookie_input:
            return _fail(self, "剪贴板是空的")
        return _ok(self, "已粘贴 %d 个字符" % len(st.cookie_input))


class NM_OT_login_cookie(NM_Base, Operator):
    bl_idname = "netease.login_cookie"
    bl_label = "使用 Cookie 登录"
    bl_description = "粘贴浏览器里的 Cookie（至少要有 MUSIC_U）后登录"

    def execute(self, context):
        st = self.st(context)
        raw = (st.cookie_input or "").strip()
        if not raw:
            return _fail(self, "请先粘贴 Cookie")
        jar = api.NeteaseClient.parse_cookie_string(raw)
        if "MUSIC_U" not in jar and "MUSIC_A" not in jar:
            return _fail(self, "Cookie 里没有找到 MUSIC_U，请确认复制的是 music.163.com 的完整 Cookie")
        runtime.save_cookie(raw, context)
        st.account_message = "已写入 Cookie，正在校验…"
        runtime.verify_login(context)
        return _ok(self, "已提交 Cookie，正在校验")


class NM_OT_open_web_login(NM_Base, Operator):
    bl_idname = "netease.open_web_login"
    bl_label = "打开网页版登录"
    bl_description = "在浏览器里打开 music.163.com 登录，再用开发者工具复制 Cookie"

    def execute(self, context):
        import webbrowser
        webbrowser.open("https://music.163.com/#/login")
        return _ok(self, "已在浏览器打开登录页")


class NM_OT_logout(NM_Base, Operator):
    bl_idname = "netease.logout"
    bl_label = "退出登录"
    bl_description = "清除本地保存的 Cookie"

    def execute(self, context):
        st = self.st(context)
        runtime.client(context).logout()
        runtime.save_cookie("", context)
        st.logged_in = False
        st.nickname = ""
        st.user_id = ""
        st.vip_text = ""
        st.account_message = "已退出登录"
        st.playlists_clear()
        runtime.clear_liked(st)
        return _ok(self, "已退出登录")


# --------------------------------------------------------------------------
# 我喜欢的音乐
# --------------------------------------------------------------------------


def _name_of(st, song_id: str) -> str:
    """在已加载的列表里找歌名，只用于提示文字。"""
    for collection in (st.tracks, st.queue):
        for item in collection:
            if item.song_id == song_id:
                return item.name
    if st.current_id == song_id:
        return st.current_name
    return ""


class NM_OT_like_track(NM_Base, Operator):
    bl_idname = "netease.like_track"
    bl_label = "加入我喜欢的音乐"
    bl_description = "把这首歌加入（或移出）「我喜欢的音乐」"

    index: IntProperty(default=-1)
    song_id: StringProperty(default="", description="直接指定歌曲 ID（列表行上的 ♥ 按钮用）")
    like: BoolProperty(default=True, description="True 加入，False 移出")

    def execute(self, context):
        st = self.st(context)
        song_id = self.song_id
        if song_id:
            name = _name_of(st, song_id)
        else:
            index = self.index if self.index >= 0 else st.track_index
            if not (0 <= index < len(st.tracks)):
                return _fail(self, "请先在列表里选一首歌")
            item = st.tracks[index]
            song_id = item.song_id
            name = item.name
        runtime.like_track(context, song_id, name, like=self.like)
        return _ok(self, "%s：%s" % ("正在加入我喜欢的音乐" if self.like else "正在取消喜欢", name or song_id))


class NM_OT_like_current(NM_Base, Operator):
    bl_idname = "netease.like_current"
    bl_label = "喜欢这首歌"
    bl_description = "把正在播放的歌加入（或移出）「我喜欢的音乐」"

    like: BoolProperty(default=True)

    def execute(self, context):
        st = self.st(context)
        if not st.current_id:
            return _fail(self, "还没有正在播放的歌曲")
        runtime.like_track(context, st.current_id, st.current_name, like=self.like)
        return _ok(self, "正在加入我喜欢的音乐" if self.like else "正在取消喜欢")


class NM_OT_refresh_liked(NM_Base, Operator):
    bl_idname = "netease.refresh_liked"
    bl_label = "同步我喜欢"
    bl_description = "重新拉取「我喜欢的音乐」全量列表，刷新列表里的 ♥ 标记"

    def execute(self, context):
        st = self.st(context)
        if not st.logged_in:
            return _fail(self, "请先用 Cookie 登录")
        runtime.refresh_liked(context)
        return _ok(self, "正在同步「我喜欢的音乐」…")


# --------------------------------------------------------------------------
# 歌单 / 推荐
# --------------------------------------------------------------------------


class NM_OT_refresh_playlists(NM_Base, Operator):
    bl_idname = "netease.refresh_playlists"
    bl_label = "刷新我的歌单"
    bl_description = "拉取账号下的歌单（含「我喜欢的音乐」）"

    def execute(self, context):
        runtime.fetch_playlists(context)
        return _ok(self, "正在获取歌单…")


class NM_OT_open_playlist(NM_Base, Operator):
    bl_idname = "netease.open_playlist"
    bl_label = "打开歌单"
    bl_description = "加载该歌单的歌曲"

    index: IntProperty(default=-1)
    load_all: BoolProperty(default=False)

    def execute(self, context):
        st = self.st(context)
        index = self.index if self.index >= 0 else st.playlist_index
        if not (0 <= index < len(st.playlists)):
            return _fail(self, "请先选择一个歌单")
        item = st.playlists[index]
        runtime.open_playlist(context, item.playlist_id, item.name, load_all=self.load_all)
        st.tab = "playlists"
        return _ok(self, "正在加载歌单：%s" % item.name)


class NM_OT_open_liked(NM_Base, Operator):
    bl_idname = "netease.open_liked"
    bl_label = "我喜欢的音乐"
    bl_description = "直接打开「我喜欢的音乐」歌单"

    def execute(self, context):
        st = self.st(context)
        for index, item in enumerate(st.playlists):
            if item.special_type == 5:
                st.playlist_index = index
                runtime.open_playlist(context, item.playlist_id, item.name)
                return _ok(self, "正在加载「我喜欢的音乐」")
        return _fail(self, "歌单列表里没有「我喜欢的音乐」，先点「刷新我的歌单」")


class NM_OT_load_all_tracks(NM_Base, Operator):
    bl_idname = "netease.load_all_tracks"
    bl_label = "补齐全部歌曲"
    bl_description = "大歌单只加载了前若干首，这里按当前列表来源补齐（可能较慢）"

    def execute(self, context):
        st = self.st(context)
        if st.list_kind != "playlist":
            return _fail(self, "只有歌单列表支持补齐")
        for item in st.playlists:
            if item.name == st.list_title:
                runtime.open_playlist(context, item.playlist_id, item.name, load_all=True)
                return _ok(self, "正在补齐歌曲…")
        return _fail(self, "找不到对应的歌单，请重新打开该歌单")


class NM_OT_load_daily(NM_Base, Operator):
    bl_idname = "netease.load_daily"
    bl_label = "每日推荐"
    bl_description = "获取每日推荐歌曲（未登录时是通用推荐）"

    def execute(self, context):
        runtime.load_daily(context)
        return _ok(self, "正在获取每日推荐…")


class NM_OT_load_radar(NM_Base, Operator):
    bl_idname = "netease.load_radar"
    bl_label = "私人雷达"
    bl_description = "定位「私人雷达」歌单并加载（需要登录）"

    def execute(self, context):
        runtime.load_radar(context)
        return _ok(self, "正在获取私人雷达…")


# --------------------------------------------------------------------------
# 播放控制
# --------------------------------------------------------------------------


class NM_OT_play_track(NM_Base, Operator):
    bl_idname = "netease.play_track"
    bl_label = "播放"
    bl_description = "播放列表里的这一首"

    index: IntProperty(default=-1)

    def execute(self, context):
        st = self.st(context)
        index = self.index if self.index >= 0 else st.track_index
        if not (0 <= index < len(st.tracks)):
            return _fail(self, "列表里没有这一项")
        # 把当前列表整体作为播放队列，便于自动下一首
        if st.list_kind != "queue":
            tracks = [item.as_dict() for item in st.tracks]
            st.queue_clear()
            for track in tracks:
                st.queue_add(track)
        st.queue_index = index
        runtime.play_track(context, st.tracks[index].as_dict())
        return _ok(self, "正在播放：%s" % st.tracks[index].name)


class NM_OT_play_queue(NM_Base, Operator):
    bl_idname = "netease.play_queue"
    bl_label = "播放队列中的这一首"
    bl_description = "从播放队列里直接播放"

    index: IntProperty(default=-1)

    def execute(self, context):
        st = self.st(context)
        index = self.index if self.index >= 0 else st.queue_index
        runtime.play_index(context, index, from_queue=True)
        return _ok(self, "正在切换歌曲")


class NM_OT_play_all(NM_Base, Operator):
    bl_idname = "netease.play_all"
    bl_label = "播放全部"
    bl_description = "把当前列表加入队列并从头播放"

    shuffle: BoolProperty(default=False)

    def execute(self, context):
        runtime.play_all(context, shuffle=self.shuffle)
        return _ok(self, "已开始播放全部")


class NM_OT_toggle_pause(NM_Base, Operator):
    bl_idname = "netease.toggle_pause"
    bl_label = "播放 / 暂停"
    bl_description = "播放或暂停当前歌曲"

    def execute(self, context):
        runtime.toggle_pause(context)
        return {"FINISHED"}


class NM_OT_stop(NM_Base, Operator):
    bl_idname = "netease.stop"
    bl_label = "停止"
    bl_description = "停止播放"

    def execute(self, context):
        runtime.stop_playback(context)
        return {"FINISHED"}


class NM_OT_next(NM_Base, Operator):
    bl_idname = "netease.next"
    bl_label = "下一首"
    bl_description = "播放队列里的下一首"

    def execute(self, context):
        runtime.next_track(context, 1)
        return {"FINISHED"}


class NM_OT_prev(NM_Base, Operator):
    bl_idname = "netease.prev"
    bl_label = "上一首"
    bl_description = "播放队列里的上一首"

    def execute(self, context):
        runtime.next_track(context, -1)
        return {"FINISHED"}


class NM_OT_seek_relative(NM_Base, Operator):
    bl_idname = "netease.seek_relative"
    bl_label = "快进 / 快退"
    bl_description = "相对当前位置跳转"

    delta: FloatProperty(default=10.0)

    def execute(self, context):
        st = self.st(context)
        if not runtime.engine().is_active:
            return _fail(self, "还没有正在播放的歌曲")
        runtime.engine().seek(max(0.0, runtime.engine().position + self.delta))
        st.position = runtime.engine().position
        return {"FINISHED"}


class NM_OT_enqueue(NM_Base, Operator):
    bl_idname = "netease.enqueue"
    bl_label = "加入播放队列"
    bl_description = "把选中的歌加入播放队列"

    index: IntProperty(default=-1)
    play_next: BoolProperty(default=False)

    def execute(self, context):
        st = self.st(context)
        runtime.enqueue_index(context, self.index if self.index >= 0 else st.track_index, self.play_next)
        return _ok(self, st.status_text)


class NM_OT_queue_remove(NM_Base, Operator):
    bl_idname = "netease.queue_remove"
    bl_label = "从队列移除"
    bl_description = "把这一首从播放队列里删掉"

    index: IntProperty(default=-1)

    def execute(self, context):
        st = self.st(context)
        index = self.index if self.index >= 0 else st.queue_index
        if not (0 <= index < len(st.queue)):
            return _fail(self, "队列里没有这一项")
        st.queue.remove(index)
        if st.queue_index >= len(st.queue):
            st.queue_index = max(0, len(st.queue) - 1)
        return _ok(self, "已从队列移除")


class NM_OT_queue_clear(NM_Base, Operator):
    bl_idname = "netease.queue_clear"
    bl_label = "清空队列"
    bl_description = "清空播放队列（不影响正在播放的歌曲）"

    def execute(self, context):
        st = self.st(context)
        st.queue_clear()
        return _ok(self, "已清空播放队列")


class NM_OT_load_lyric(NM_Base, Operator):
    bl_idname = "netease.load_lyric"
    bl_label = "加载歌词"
    bl_description = "获取当前歌曲的歌词（同时建立动态歌词的时间轴）"

    force: BoolProperty(default=False, description="已经有歌词时也重新拉一次")

    def execute(self, context):
        st = self.st(context)
        if not st.current_id:
            return _fail(self, "还没有正在播放的歌曲")
        runtime.load_lyric(context, st.current_id, force=self.force)
        st.show_lyric = True
        return _ok(self, "正在加载歌词…")


# --------------------------------------------------------------------------
# 动态歌词浮层
# --------------------------------------------------------------------------

#: 拖动过程中的原始位置（用于右键取消）
_DRAG = {"origin": None}


class NM_OT_toggle_lyric_overlay(NM_Base, Operator):
    bl_idname = "netease.toggle_lyric_overlay"
    bl_label = "动态歌词浮层"
    bl_description = "在 3D 视图里显示/隐藏跟随播放滚动的歌词（快捷键 Ctrl+Alt+L）"

    mode: EnumProperty(
        name="模式",
        items=[("toggle", "切换", ""), ("on", "打开", ""), ("off", "关闭", "")],
        default="toggle",
    )

    def execute(self, context):
        st = self.st(context)
        if self.mode == "on":
            st.lyric_overlay = True
        elif self.mode == "off":
            st.lyric_overlay = False
        else:
            st.lyric_overlay = not st.lyric_overlay
        if st.lyric_overlay:
            # 打开时如果没有歌词就顺手取一次，避免“打开了却没东西”
            if st.current_id and not runtime.lyric_timeline():
                runtime.load_lyric(context, st.current_id)
            elif not st.current_id:
                st.set_status("浮层已打开，播放一首歌就会显示歌词")
            runtime.redraw()
        return _ok(self, "动态歌词浮层%s" % ("已打开" if st.lyric_overlay else "已关闭"))


class NM_OT_drag_lyric(NM_Base, Operator):
    bl_idname = "netease.drag_lyric"
    bl_label = "拖动歌词位置"
    bl_description = "移动鼠标把歌词浮层拖到想要的位置：左键确认，右键/ESC 取消，方向键微调"

    def invoke(self, context, event):
        st = self.st(context)
        if not st.lyric_overlay:
            st.lyric_overlay = True
        _DRAG["origin"] = (st.lyric_pos_x, st.lyric_pos_y)
        st.lyric_drag_active = True
        context.window_manager.modal_handler_add(self)
        self._hint(context, "移动鼠标调整歌词位置：左键确认，右键/ESC 取消，方向键微调（Shift 加速）")
        runtime.redraw()
        return {"RUNNING_MODAL"}

    @staticmethod
    def _hint(context, text):
        try:
            context.workspace.status_text_set(text)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _window_region(context):
        area = getattr(context, "area", None)
        if area is None:
            return None
        for region in area.regions:
            if region.type == "WINDOW":
                return region
        return None

    def _follow(self, context, event):
        st = self.st(context)
        region = self._window_region(context)
        if region is None or region.width <= 0 or region.height <= 0:
            return
        st.lyric_pos_x = overlay.clamp_position((event.mouse_x - region.x) / region.width)
        st.lyric_pos_y = overlay.clamp_position((event.mouse_y - region.y) / region.height)
        runtime.redraw()

    def modal(self, context, event):
        st = self.st(context)
        if event.type == "MOUSEMOVE":
            self._follow(context, event)
        elif event.type == "LEFTMOUSE" and event.value == "PRESS":
            st.lyric_drag_active = False
            self._hint(context, None)
            return {"FINISHED"}
        elif event.type in {"RIGHTMOUSE", "ESC"}:
            if _DRAG["origin"]:
                st.lyric_pos_x, st.lyric_pos_y = _DRAG["origin"]
            st.lyric_drag_active = False
            self._hint(context, None)
            runtime.redraw()
            return {"CANCELLED"}
        elif event.type in {"LEFT", "RIGHT", "UP", "DOWN"} and event.value == "PRESS":
            step = 0.012 if event.shift else 0.004
            dx = -step if event.type == "LEFT" else (step if event.type == "RIGHT" else 0.0)
            dy = -step if event.type == "DOWN" else (step if event.type == "UP" else 0.0)
            st.lyric_pos_x = overlay.clamp_position(st.lyric_pos_x + dx)
            st.lyric_pos_y = overlay.clamp_position(st.lyric_pos_y + dy)
            runtime.redraw()
        return {"RUNNING_MODAL"}


class NM_OT_reset_lyric_pos(NM_Base, Operator):
    bl_idname = "netease.reset_lyric_pos"
    bl_label = "重置歌词位置"
    bl_description = "把歌词浮层放回默认位置"

    def execute(self, context):
        st = self.st(context)
        st.lyric_pos_x = 0.5
        st.lyric_pos_y = 0.20
        runtime.redraw()
        return _ok(self, "歌词位置已重置")


class NM_OT_fix_audio_stutter(NM_Base, Operator):
    bl_idname = "netease.fix_audio_stutter"
    bl_label = "一键抗卡顿"
    bl_description = ("渲染（尤其 Cycles）时音频爆音/卡顿？这会把音频缓冲加大、打开内存缓存、"
                      "并优先使用 OpenAL 后端；随时可以在偏好设置里改回去")

    def execute(self, context):
        settings = runtime.prefs(context)
        if settings is None:
            return _fail(self, "读不到插件偏好设置")
        settings.audio_buffer_frames = "8192"
        settings.ram_cache_sound = True
        settings.audio_backend = "OpenAL"
        runtime.apply_audio_settings(context)
        engine = runtime.engine()
        if engine.is_active and (engine.backend, engine.buffer_frames) != ("OpenAL", 8192):
            return _ok(self, "已改为抗卡顿配置，当前这首歌放完后生效")
        if not engine.ensure_device():
            return _fail(self, engine.last_error or "打开音频设备失败")
        return _ok(self, "已切到抗卡顿配置：%s" % (engine.last_device or "已应用"))


class NM_OT_trim_cache(NM_Base, Operator):
    bl_idname = "netease.trim_cache"
    bl_label = "立即清理到上限"
    bl_description = "按偏好设置里的数量上限删掉最久没用的缓存文件"

    def execute(self, context):
        st = self.st(context)
        settings = runtime.prefs(context)
        limit = int(getattr(settings, "cache_limit_count", 0) or 0)
        keep = runtime.engine().path
        deleted, freed = player.enforce_cache_limit(runtime.cache_dir(context), limit, keep=(keep,))
        runtime.refresh_cache_text(st)
        if not deleted:
            return _ok(self, "缓存已经在 %d 个以内" % limit)
        return _ok(self, "已清理 %d 个文件，释放 %s" % (deleted, player.human_size(freed)))


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


class NM_OT_open_in_browser(NM_Base, Operator):
    bl_idname = "netease.open_in_browser"
    bl_label = "在网页版打开"
    bl_description = "在浏览器里打开这首歌 / 这个歌单"

    what: StringProperty(default="song")

    def execute(self, context):
        import webbrowser
        st = self.st(context)
        if self.what == "playlist" and len(st.playlists) and 0 <= st.playlist_index < len(st.playlists):
            url = "https://music.163.com/#/playlist?id=%s" % st.playlists[st.playlist_index].playlist_id
        elif st.current_id:
            url = "https://music.163.com/#/song?id=%s" % st.current_id
        else:
            return _fail(self, "没有可打开的歌曲")
        webbrowser.open(url)
        return _ok(self, "已在浏览器打开")


class NM_OT_open_cache(NM_Base, Operator):
    bl_idname = "netease.open_cache"
    bl_label = "打开缓存目录"
    bl_description = "打开存放已下载音频的缓存目录"

    def execute(self, context):
        path = runtime.cache_dir(context)
        utils.ensure_dir(path)
        player.open_in_system_player(path)
        return _ok(self, "已打开 %s" % path)


class NM_OT_clear_cache(NM_Base, Operator):
    bl_idname = "netease.clear_cache"
    bl_label = "清空缓存"
    bl_description = "删除所有已下载的音频文件"

    def execute(self, context):
        count, size = player.clear_cache(runtime.cache_dir(context))
        runtime.refresh_cache_text()
        return _ok(self, "已删除 %d 个文件，释放 %s" % (count, player.human_size(size)))


class NM_OT_play_in_system(NM_Base, Operator):
    bl_idname = "netease.play_in_system"
    bl_label = "用系统播放器打开"
    bl_description = "当 Blender 内的 aud 播放不可用时，用系统播放器播放当前歌曲"

    def execute(self, context):
        st = self.st(context)
        session = runtime.client(context)
        st.set_status("正在获取播放地址…")
        song_id = st.current_id or (st.tracks[st.track_index].song_id if len(st.tracks) else "")

        def work():
            info = session.song_url(song_id, runtime.quality(context))
            dest = player.cache_path(runtime.cache_dir(context), song_id, info.get("requested_level") or "", info.get("url") or "")
            if not (os.path.isfile(dest) and os.path.getsize(dest) > 0):
                player.download(info.get("url") or "", dest)
            return dest

        def done(path):
            if player.open_in_system_player(path):
                st.set_status("已交给系统播放器")
            else:
                st.set_error("打开系统播放器失败")

        jobs.submit("交给系统播放器", work, on_done=done, on_error=lambda exc: st.set_error(exc))
        return _ok(self, "正在准备音频…")


class NM_OT_self_test(NM_Base, Operator):
    bl_idname = "netease.self_test"
    bl_label = "运行自检"
    bl_description = "检查加密实现是否与官方测试向量一致，并测试三套接口的连通性"

    def execute(self, context):
        st = self.st(context)
        lines = ["== 加密测试向量 =="]
        failed = 0
        for name, (ok, detail) in crypto.selftest().items():
            lines.append("  [%s] %s%s" % ("通过" if ok else "失败", name, "" if ok else "  " + detail))
            failed += 0 if ok else 1
        lines.append("  aud 模块：%s" % ("可用" if player.aud_available() else player.aud_error()))
        engine = runtime.engine()
        engine.ensure_device()          # 顺便把设备打开，好把真实后端/缓冲报出来
        lines.append("  音频设备：%s" % (engine.last_device or engine.last_error or "尚未打开"))
        lines.append("  内存缓存：%s（约 %.0f MB/分钟）"
                     % ("开" if engine.ram_cache else "关", player.RAM_PER_MINUTE_MB))
        if engine.last_error and engine.last_device:
            lines.append("  设备提示：%s" % engine.last_error)
        lines.append("")
        lines.append("== 接口连通性 ==")
        session = runtime.client(context)

        def work():
            report = []
            for label, fn in (
                ("登录状态查询", session.login_status),
                ("每日推荐", lambda: "%d 首" % len(session.daily_recommend())),
                ("歌单详情", lambda: session.playlist_detail("3778678", track_limit=5)["name"]),
            ):
                try:
                    report.append("  %s：%s" % (label, fn()))
                except Exception as exc:  # noqa: BLE001
                    report.append("  %s：失败 %s" % (label, exc))
            for transport, path, result in session.transport_log[-12:]:
                report.append("  [%s] %s → %s" % (transport, path, result))
            return report

        def done(report):
            st.self_test_report = "\n".join(lines + report)
            st.set_status("自检完成")

        jobs.submit("接口自检", work, on_done=done, on_error=lambda exc: st.set_error(exc))
        st.self_test_report = "\n".join(lines + ["  （正在测试接口连通性…）"])
        return _ok(self, "自检完成" if not failed else "加密自检有失败项")


CLASSES = (
    NM_OT_refresh_account,
    NM_OT_login_cookie,
    NM_OT_paste_cookie,
    NM_OT_open_web_login,
    NM_OT_logout,
    NM_OT_like_track,
    NM_OT_like_current,
    NM_OT_refresh_liked,
    NM_OT_refresh_playlists,
    NM_OT_open_playlist,
    NM_OT_open_liked,
    NM_OT_load_all_tracks,
    NM_OT_load_daily,
    NM_OT_load_radar,
    NM_OT_play_track,
    NM_OT_play_queue,
    NM_OT_play_all,
    NM_OT_toggle_pause,
    NM_OT_stop,
    NM_OT_next,
    NM_OT_prev,
    NM_OT_seek_relative,
    NM_OT_enqueue,
    NM_OT_queue_remove,
    NM_OT_queue_clear,
    NM_OT_load_lyric,
    NM_OT_toggle_lyric_overlay,
    NM_OT_drag_lyric,
    NM_OT_reset_lyric_pos,
    NM_OT_trim_cache,
    NM_OT_fix_audio_stutter,
    NM_OT_open_in_browser,
    NM_OT_open_cache,
    NM_OT_clear_cache,
    NM_OT_play_in_system,
    NM_OT_self_test,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # noqa: BLE001
            pass
