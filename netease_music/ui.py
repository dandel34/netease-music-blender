# -*- coding: utf-8 -*-
"""界面：3D 视图侧栏里的「网易云音乐」面板 + 两个列表控件 + ♥ 收藏按钮。

面板本身不发起任何网络请求，只读取 :mod:`runtime` 里的状态；按钮都指向
:mod:`ops` 里的操作符，因此重画面板永远不会卡住界面。

图标一律取自 Blender 内置图标表（已逐个校验存在），避免出现“图标找不到”。
"""

from __future__ import annotations

import bpy
from bpy.types import Panel, UIList

from . import overlay, player, runtime, utils

TABS = ("account", "player", "playlists", "recommend")


# --------------------------------------------------------------------------
# 列表
# --------------------------------------------------------------------------


class NM_UL_playlists(UIList):
    bl_idname = "NM_UL_playlists"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        special = item.special_type == 5
        mark = "♥" if special else ("＋" if item.subscribed else "·")
        op = row.operator("netease.open_playlist", text="%s %s" % (mark, item.name), emboss=False,
                          icon="HEART" if special else "FILE_FOLDER")
        op.index = index
        row.label(text=str(item.track_count))


class NM_UL_tracks(UIList):
    bl_idname = "NM_UL_tracks"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        is_queue = active_propname == "queue_index"
        row = layout.row(align=True)
        if is_queue:
            playing = index == data.queue_index
            op = row.operator("netease.play_queue", text="", emboss=False,
                              icon="SOUND" if playing else "PLAY")
        else:
            op = row.operator("netease.play_track", text="", emboss=False, icon="PLAY")
        op.index = index

        # ♥ 收藏开关：已收藏是实心图标，未收藏是空心字符，点击即加入/移出「我喜欢的音乐」
        if item.liked:
            heart = row.operator("netease.like_track", text="", emboss=False, icon="HEART")
            heart.like = False
        else:
            heart = row.operator("netease.like_track", text="♥", emboss=False)
            heart.like = True
        heart.song_id = item.song_id
        heart.index = index

        row.label(text="%d. %s" % (index + 1, item.name))
        sub = row.row(align=True)
        sub.alignment = "RIGHT"
        sub.label(text=item.artists if len(item.artists) < 18 else item.artists[:16] + "…")
        sub.label(text=item.duration_text or utils.format_duration(item.duration))
        if is_queue:
            remove = row.operator("netease.queue_remove", text="", emboss=False, icon="X")
            remove.index = index


# --------------------------------------------------------------------------
# 面板
# --------------------------------------------------------------------------


class NM_PT_main(Panel):
    bl_idname = "NM_PT_main"
    bl_label = "网易云音乐"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "网易云音乐"

    # ------------------------------------------------------------ 公共片段

    def draw(self, context):
        layout = self.layout
        st = context.scene.netease

        self._header(layout, st)
        layout.prop(st, "tab", expand=True)
        layout.separator()

        if st.tab == "account":
            self._account(layout, st)
        elif st.tab == "player":
            self._player(layout, st)
        elif st.tab == "playlists":
            self._playlists(layout, st)
        else:
            self._recommend(layout, st)

        self._footer(layout, st)

    # ------------------------------------------------------------ 顶部

    def _header(self, layout, st):
        box = layout.box()
        row = box.row(align=True)
        if st.logged_in:
            row.label(text="已登录：%s" % (st.nickname or st.user_id), icon="USER")
        else:
            row.label(text="未登录", icon="USER")
        row.operator("netease.refresh_account", text="", icon="FILE_REFRESH")
        if st.logged_in and st.vip_text:
            box.label(text=st.vip_text, icon="CHECKMARK")

    # ------------------------------------------------------------ 账号（仅 Cookie 登录）

    def _account(self, layout, st):
        box = layout.box()
        box.label(text="Cookie 登录", icon="PASTEDOWN")
        col = box.column(align=True)
        col.label(text="1. 点下面「打开登录页」在浏览器登录")
        col.label(text="2. F12 → 应用/Application → Cookies")
        col.label(text="3. 全选复制（要有 MUSIC_U）")
        col.label(text="4. 回来粘贴 → 点「使用 Cookie 登录」")
        row = box.row(align=True)
        row.operator("netease.paste_cookie", icon="COPYDOWN")
        row.operator("netease.open_web_login", text="打开登录页", icon="URL")
        box.prop(st, "cookie_input", text="")
        box.operator("netease.login_cookie", icon="IMPORT")

        if st.logged_in:
            box = layout.box()
            box.label(text="账号", icon="USER")
            box.label(text="%s（ID %s）" % (st.nickname or "无昵称", st.user_id or "?"))
            if st.vip_text:
                box.label(text=st.vip_text, icon="CHECKMARK")
            row = box.row(align=True)
            row.operator("netease.refresh_playlists", icon="FILE_REFRESH")
            row.operator("netease.logout", icon="TRASH")

        # 我喜欢的音乐
        box = layout.box()
        row = box.row(align=True)
        row.label(text="我喜欢的音乐", icon="HEART")
        row.label(text=st.liked_text or "")
        row = box.row(align=True)
        row.operator("netease.open_liked", text="打开歌单", icon="FILE_FOLDER")
        row.operator("netease.refresh_liked", text="同步", icon="FILE_REFRESH")
        box.label(text="列表里每首歌左边的 ♥ 可直接加入或移出", icon="INFO")

        if st.account_message:
            layout.label(text=st.account_message[:60], icon="INFO")

    # ------------------------------------------------------------ 播放

    def _player(self, layout, st):
        box = layout.box()
        if st.current_id:
            row = box.row(align=True)
            row.label(text=st.current_name or "未知歌曲", icon="SOUND")
            if st.current_liked:
                unlike = row.operator("netease.like_current", text="", emboss=False, icon="HEART")
                unlike.like = False
            else:
                like = row.operator("netease.like_current", text="♥", emboss=False)
                like.like = True
            box.label(text=st.current_artists or "", icon="USER")
            if st.current_album:
                box.label(text=st.current_album, icon="FILE_FOLDER")
            if st.current_source:
                box.label(text=st.current_source, icon="SETTINGS")
            row = box.row(align=True)
            if st.current_liked:
                btn = row.operator("netease.like_current", text="已在我喜欢的音乐里", icon="CHECKMARK")
                btn.like = False
            else:
                btn = row.operator("netease.like_current", text="加入我喜欢的音乐", icon="ADD")
                btn.like = True
        else:
            box.label(text="还没有播放任何歌曲", icon="INFO")
            box.label(text="到「歌单」或「推荐」里点 ▶ 即可", icon="INFO")

        text = "%s / %s" % (utils.format_duration(st.position * 1000),
                            utils.format_duration((st.duration or 0) * 1000))
        try:
            box.progress(factor=st.progress_ratio, text=text)
        except Exception:  # noqa: BLE001
            box.label(text=text)
        box.prop(st, "seek_seconds", slider=True)

        row = box.row(align=True)
        back = row.operator("netease.seek_relative", text="", icon="REW")
        back.delta = -10.0
        row.operator("netease.prev", text="", icon="PREV_KEYFRAME")
        row.operator("netease.toggle_pause", text="", icon="PAUSE" if st.is_playing else "PLAY")
        row.operator("netease.stop", text="", icon="FULLSCREEN_EXIT")
        row.operator("netease.next", text="", icon="NEXT_KEYFRAME")
        fwd = row.operator("netease.seek_relative", text="", icon="FF")
        fwd.delta = 10.0

        row = box.row(align=True)
        row.prop(st, "volume", slider=True)
        row.prop(st, "loop_mode", text="")

        if st.downloading:
            dbox = layout.box()
            dbox.label(text=st.download_text or "正在下载…", icon="IMPORT")
            dbox.progress(factor=st.download_progress, text="%.0f%%" % (st.download_progress * 100))

        # 歌词（当前行）+ 动态歌词浮层
        self._lyric_text(layout, st)
        self._lyric_overlay(layout, st)

        # 队列
        box = layout.box()
        row = box.row(align=True)
        row.label(text="播放队列（%d）" % len(st.queue), icon="SOUND")
        row.operator("netease.queue_clear", text="", icon="TRASH")
        box.template_list("NM_UL_tracks", "queue", st, "queue", st, "queue_index", rows=5)
        row = box.row(align=True)
        row.operator("netease.play_queue", text="播放选中", icon="PLAY")
        if len(st.tracks):
            enq = row.operator("netease.enqueue", text="加入选中", icon="ADD")
            enq.play_next = True

    # ------------------------------------------------------------ 动态歌词浮层

    def _lyric_overlay(self, layout, st):
        settings = runtime.prefs(context=bpy.context)

        box = layout.box()
        row = box.row(align=True)
        row.label(text="动态歌词浮层", icon="FONT_DATA")
        toggle = row.operator(
            "netease.toggle_lyric_overlay", text="",
            icon="HIDE_OFF" if st.lyric_overlay else "HIDE_ON",
        )
        toggle.mode = "toggle"

        if st.lyric_overlay:
            box.label(text="已开启：跟随播放滚动，可拖动位置", icon="CHECKMARK")
        else:
            box.label(text="未开启（Ctrl+Alt+L 也可以开关）", icon="INFO")

        info = "%d 行" % st.lyric_count if st.lyric_count else "暂无歌词"
        if runtime.lyric_song_id() and st.current_id and runtime.lyric_song_id() != st.current_id:
            info = "歌词与当前歌曲不匹配，点刷新"
        box.label(text="歌词：%s" % info, icon="TEXT")

        row = box.row(align=True)
        drag = row.operator("netease.drag_lyric", text="拖动位置", icon="HAND")
        row.operator("netease.reset_lyric_pos", text="", icon="LOOP_BACK")
        reload_lyric = row.operator("netease.load_lyric", text="", icon="FILE_REFRESH")
        reload_lyric.force = True

        col = box.column(align=True)
        col.prop(st, "lyric_pos_x", slider=True)
        col.prop(st, "lyric_pos_y", slider=True)

        if settings is not None:
            col = box.column(align=True)
            col.prop(settings, "lyric_font_size")
            col.prop(settings, "lyric_bg_opacity")
            row = col.row(align=True)
            row.prop(settings, "lyric_show_translation")
            row.prop(settings, "lyric_show_title")

        error = overlay.draw_error()
        if error:
            box.label(text="绘制出错：%s" % error[:60], icon="ERROR")

    # ------------------------------------------------------------ 歌词文本

    def _lyric_text(self, layout, st):
        box = layout.box()
        row = box.row(align=True)
        row.label(text="歌词", icon="TEXT")
        row.operator("netease.load_lyric", text="", icon="FILE_REFRESH")
        row.prop(st, "show_lyric", text="", icon="HIDE_OFF" if st.show_lyric else "HIDE_ON")

        if st.lyric_line:
            box.label(text=st.lyric_line[:70])
            if st.lyric_translation:
                box.label(text=st.lyric_translation[:70], icon="BOOKMARKS")
            if st.lyric_next:
                box.label(text="下一句：%s" % st.lyric_next[:60], icon="FORWARD")
        elif st.current_id:
            box.label(text="还没有歌词（播放时会自动加载）", icon="INFO")

        if st.show_lyric and st.lyric:
            lines = [line for line in st.lyric.splitlines() if line.strip()][:14]
            for line in lines:
                box.label(text=line[:70])
            if len(st.lyric.splitlines()) > 14:
                box.label(text="……（只显示前 14 行）", icon="INFO")

    # ------------------------------------------------------------ 歌单

    def _playlists(self, layout, st):
        box = layout.box()
        row = box.row(align=True)
        row.label(text="我的歌单（%d）" % len(st.playlists), icon="FILE_FOLDER")
        row.operator("netease.refresh_playlists", text="", icon="FILE_REFRESH")
        if not st.logged_in:
            box.label(text="登录后才能看到自己的歌单", icon="INFO")
        box.template_list("NM_UL_playlists", "playlists", st, "playlists", st, "playlist_index", rows=6)

        row = box.row(align=True)
        row.operator("netease.open_playlist", text="打开歌单", icon="IMPORT")
        row.operator("netease.open_liked", text="我喜欢的音乐", icon="HEART")

        self._track_list(layout, st)

    # ------------------------------------------------------------ 推荐

    def _recommend(self, layout, st):
        box = layout.box()
        box.label(text="网易云每日更新", icon="LIGHT_SUN")
        row = box.row(align=True)
        row.operator("netease.load_daily", text="每日推荐", icon="LIGHT_SUN")
        row.operator("netease.load_radar", text="私人雷达", icon="CAMERA_DATA")
        box.label(text="私人雷达需要登录；未登录时可用每日推荐", icon="INFO")
        if st.radar_source:
            box.label(text=st.radar_source[:60], icon="INFO")

        self._track_list(layout, st)

    # ------------------------------------------------------------ 歌曲列表

    def _track_list(self, layout, st):
        box = layout.box()
        row = box.row(align=True)
        row.label(text=st.list_title[:32] or "歌曲列表", icon="PRESET")
        row.label(text="%d 首" % len(st.tracks))
        if st.list_note:
            box.label(text=st.list_note[:60], icon="INFO")
        box.template_list("NM_UL_tracks", "tracks", st, "tracks", st, "track_index", rows=8)

        row = box.row(align=True)
        row.operator("netease.play_track", text="播放选中", icon="PLAY")
        row.operator("netease.play_all", text="播放全部", icon="PLAY")

        row = box.row(align=True)
        shuffle = row.operator("netease.play_all", text="随机播放", icon="SORTBYEXT")
        shuffle.shuffle = True
        enq = row.operator("netease.enqueue", text="加入队列", icon="ADD")
        enq.play_next = False
        nxt = row.operator("netease.enqueue", text="下一首播放", icon="FORWARD")
        nxt.play_next = True

        row = box.row(align=True)
        like = row.operator("netease.like_track", text="加入我喜欢的音乐", icon="HEART")
        like.like = True
        unlike = row.operator("netease.like_track", text="取消喜欢", icon="X")
        unlike.like = False

        if st.list_kind == "playlist":
            box.operator("netease.load_all_tracks", icon="IMPORT")

    # ------------------------------------------------------------ 底部状态

    def _footer(self, layout, st):
        box = layout.box()
        busy = runtime.jobs.status_text()
        if busy:
            box.label(text=busy[:60], icon="TIME")
        if st.status_text:
            box.label(text=st.status_text[:70], icon="INFO")
        if st.error_text:
            col = box.column(align=True)
            for line in st.error_text.splitlines()[:4]:
                col.label(text=line[:70], icon="ERROR")
        row = box.row(align=True)
        row.label(text="缓存：%s" % (st.cache_text or "—"), icon="DISK_DRIVE")
        row.operator("netease.open_cache", text="", icon="FILE_FOLDER")
        if not player.aud_available():
            box.operator("netease.play_in_system", icon="PLAY")


class NM_PT_selftest(Panel):
    bl_idname = "NM_PT_selftest"
    bl_label = "自检报告"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "网易云音乐"
    bl_parent_id = "NM_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return bool(context.scene.netease.self_test_report)

    def draw(self, context):
        st = context.scene.netease
        layout = self.layout
        for line in st.self_test_report.splitlines()[:30]:
            layout.label(text=line[:80])
        layout.operator("netease.self_test", icon="CHECKMARK")


CLASSES = (NM_UL_playlists, NM_UL_tracks, NM_PT_main, NM_PT_selftest)


def register():
    for cls in CLASSES:
        try:
            bpy.utils.register_class(cls)
        except Exception as exc:  # noqa: BLE001 - 单个界面类失败不该拖垮整个插件
            utils.log("注册界面类失败 %s：%s" % (getattr(cls, "__name__", cls), exc))


def unregister():
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # noqa: BLE001
            pass
