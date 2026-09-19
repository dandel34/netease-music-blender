# -*- coding: utf-8 -*-
"""插件运行时：会话、播放引擎、后台任务回收、播放队列与自动下一首。

界面（ui.py）只负责画，操作符（ops.py）只负责转发，真正的状态机都在这里。
主线程定时器调用 :func:`tick`，它做三件事：

1. 回收后台线程的结果（网络请求都是异步的，界面不会卡住）；
2. 同步播放进度、应用跳转请求、检测播放结束并自动下一首；
3. 刷新下载进度与缓存信息，并在需要时请求面板重画。

「我喜欢的音乐」（♥）在这里维护：登录后拉一次全量 ID 作本地缓存，
收藏/取消收藏后本地同步，避免每画一次列表都去问服务器。
"""

from __future__ import annotations

import os
import time

import bpy

from . import api, jobs, lyrics as lyrics_mod, player, props, utils

# --------------------------------------------------------------------------
# 单例
# --------------------------------------------------------------------------

_ENGINE = player.AudioEngine()
_CLIENT = None
_CLIENT_KEY = None

#: 下载进度（子线程写、主线程读）
DOWNLOAD = {"done": 0, "total": 0, "active": False, "name": ""}
#: 播放过程的辅助状态
PLAYBACK = {"started": 0.0, "loading": False, "manual_stop": False, "last_auto_next": 0.0}
#: 「我喜欢的音乐」ID 集合（登录后同步一次，点赞后本地同步，避免每次都问服务器）
LIKED = {"ids": set(), "loaded": False, "uid": ""}
#: 当前歌曲的歌词时间轴（浮层与面板共用；只有一份，切歌就换）
LYRIC = {"song_id": "", "timeline": [], "loading": False}


def state(context=None) -> "props.NM_State":
    context = context or bpy.context
    return context.scene.netease


def prefs(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def engine() -> player.AudioEngine:
    return _ENGINE


def invalidate_client():
    global _CLIENT, _CLIENT_KEY
    _CLIENT = None
    _CLIENT_KEY = None


def client(context=None) -> api.NeteaseClient:
    """按偏好设置构造会话；Cookie / 协议变化时自动重建。"""
    global _CLIENT, _CLIENT_KEY
    settings = prefs(context)
    if settings is None:
        key = ("default",)
        if _CLIENT is None:
            _CLIENT = api.NeteaseClient()
            _CLIENT_KEY = key
        return _CLIENT
    key = (settings.cookie, settings.real_ip, settings.preferred_transport, settings.timeout, settings.debug_requests)
    if _CLIENT is None or _CLIENT_KEY != key:
        _CLIENT = api.NeteaseClient(
            cookie=settings.cookie,
            real_ip=settings.real_ip,
            preferred=settings.preferred_transport,
            timeout=settings.timeout,
            debug=settings.debug_requests,
        )
        _CLIENT_KEY = key
    return _CLIENT


def save_cookie(cookie: str, context=None):
    settings = prefs(context)
    if settings is not None:
        settings.cookie = cookie or ""
    invalidate_client()


def quality(context=None) -> str:
    settings = prefs(context)
    return getattr(settings, "quality", "exhigh") if settings else "exhigh"


def cache_dir(context=None) -> str:
    settings = prefs(context)
    path = getattr(settings, "cache_dir", "") if settings else ""
    return path or utils.default_cache_dir()


def enforce_cache_limit(context, keep: str = ""):
    """按偏好设置里的数量上限清理缓存（超出就删最旧的，正在播的那首不动）。"""
    settings = prefs(context)
    if settings is None or not getattr(settings, "cache_limit_enabled", False):
        return
    limit = int(getattr(settings, "cache_limit_count", 0) or 0)
    if limit <= 0:
        return
    deleted, freed = player.enforce_cache_limit(cache_dir(context), limit, keep=(keep,))
    if deleted:
        utils.log("缓存超过上限 %d 个，已清理 %d 个旧文件，释放 %s"
                  % (limit, deleted, player.human_size(freed)))
        refresh_cache_text(state(context))


# --------------------------------------------------------------------------
# 歌词：面板与动态浮层共用同一份时间轴
# --------------------------------------------------------------------------


def clear_lyric(st):
    LYRIC.update({"song_id": "", "timeline": []})
    st.lyric = ""
    st.lyric_line = ""
    st.lyric_translation = ""
    st.lyric_next = ""
    st.lyric_index = -1
    st.lyric_count = 0


def set_lyric(st, payload: dict, song_id: str):
    timeline = lyrics_mod.build_timeline(payload.get("lrc") or "", payload.get("translated") or "")
    LYRIC.update({"song_id": str(song_id), "timeline": timeline})
    st.lyric = payload.get("merged") or "（这首歌没有歌词）"
    st.lyric_count = len(timeline)
    update_lyric_position(st)


def load_lyric(context, song_id: str = "", force: bool = False):
    """拉歌词并解析时间轴；面板与浮层都用这一条路径。"""
    st = state(context)
    song_id = str(song_id or st.current_id or "")
    if not song_id:
        st.set_error("还没有正在播放的歌曲")
        return
    if not force and LYRIC["song_id"] == song_id and LYRIC["timeline"]:
        return
    if LYRIC["loading"]:
        return
    session = client(context)
    LYRIC["loading"] = True
    st.set_status("正在加载歌词…")

    def work():
        return session.lyric(song_id)

    def done(payload):
        LYRIC["loading"] = False
        if st.current_id and song_id != st.current_id:
            return                     # 已经切歌，丢弃这次结果
        set_lyric(st, payload, song_id)
        if LYRIC["timeline"]:
            st.set_status("歌词已加载：%d 行" % len(LYRIC["timeline"]))
        else:
            st.set_status("这首歌没有可解析的歌词")

    def failed(exc):
        LYRIC["loading"] = False
        st.set_error("加载歌词失败：%s" % exc)

    jobs.submit("加载歌词", work, on_done=done, on_error=failed)


def auto_lyric_enabled(context) -> bool:
    settings = prefs(context)
    if settings is not None and getattr(settings, "auto_load_lyric", True):
        return True
    return bool(state(context).lyric_overlay)


def update_lyric_position(st):
    """按播放位置更新“当前行”，浮层和面板都读这几个属性。"""
    timeline = LYRIC["timeline"]
    if not timeline:
        if st.lyric_line or st.lyric_index != -1:
            st.lyric_line = ""
            st.lyric_translation = ""
            st.lyric_next = ""
            st.lyric_index = -1
        return
    text, translation, following = lyrics_mod.current_text(timeline, st.position)
    st.lyric_line = text
    st.lyric_translation = translation
    st.lyric_next = following
    st.lyric_index = lyrics_mod.index_at(timeline, st.position)


def lyric_timeline() -> list:
    return LYRIC["timeline"]


def lyric_window(st, above: int = 1, below: int = 2) -> dict:
    return lyrics_mod.window(LYRIC["timeline"], st.position, above, below)


def lyric_song_id() -> str:
    return LYRIC["song_id"]


def redraw():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 列表装载
# --------------------------------------------------------------------------


def load_tracks_into_state(st, tracks, title: str, kind: str, note: str = ""):
    st.tracks_clear()
    for track in tracks:
        st.tracks_add(track)
    st.list_title = title
    st.list_kind = kind
    st.list_note = note
    apply_liked_flags(st)
    st.set_status("已加载 %d 首：%s" % (len(tracks), title))


# --------------------------------------------------------------------------
# 「我喜欢的音乐」
# --------------------------------------------------------------------------


def liked_playlist_id(st) -> str:
    """账号里「我喜欢的音乐」的歌单 ID（用于备用接口）。"""
    for item in st.playlists:
        if item.special_type == 5:
            return item.playlist_id
    return ""


def apply_liked_flags(st):
    """按本地缓存刷新列表里的 ♥ 标记。"""
    ids = LIKED["ids"]
    for collection in (st.tracks, st.queue):
        for item in collection:
            item.liked = item.song_id in ids
    st.current_liked = bool(st.current_id) and st.current_id in ids
    st.liked_count = len(ids)
    st.liked_loaded = bool(LIKED["loaded"])
    st.liked_text = ("已同步 %d 首" % len(ids)) if LIKED["loaded"] else "尚未同步"


def clear_liked(st):
    LIKED.update({"ids": set(), "loaded": False, "uid": ""})
    apply_liked_flags(st)


def refresh_liked(context, silent: bool = False):
    """登录后拉一次「我喜欢的音乐」全量 ID，用来给列表打 ♥。"""
    st = state(context)
    if not st.logged_in or not st.user_id:
        clear_liked(st)
        return
    session = client(context)
    uid = st.user_id
    if not silent:
        st.set_status("正在同步「我喜欢的音乐」…")

    def work():
        return session.likelist(uid)

    def done(ids):
        LIKED.update({"ids": set(ids), "loaded": True, "uid": uid})
        apply_liked_flags(st)
        st.set_status("已同步「我喜欢的音乐」：%d 首" % len(ids))

    def failed(exc):
        st.liked_text = "同步失败：%s" % str(exc)[:60]
        if not silent:
            st.set_error("读取「我喜欢的音乐」失败：%s" % exc)

    jobs.submit("同步我喜欢", work, on_done=done, on_error=failed)


def like_track(context, song_id: str, name: str = "", like: bool = True):
    """把一首歌加入 / 移出「我喜欢的音乐」。"""
    st = state(context)
    song_id = str(song_id or "")
    if not song_id:
        st.set_error("没有可操作的歌曲")
        return
    if not st.logged_in:
        st.set_error("请先登录（Cookie 导入）后再把歌曲加入「我喜欢的音乐」")
        return
    session = client(context)
    playlist_id = liked_playlist_id(st)
    st.clear_error()
    st.set_status("正在%s：%s" % ("加入我喜欢的音乐" if like else "取消喜欢", name or song_id))

    def work():
        return session.set_like(song_id, like, playlist_id)

    def done(result):
        if like:
            LIKED["ids"].add(song_id)
        else:
            LIKED["ids"].discard(song_id)
        LIKED["loaded"] = True
        apply_liked_flags(st)
        st.set_status("%s成功（%s）：%s"
                      % ("已加入我喜欢的音乐" if like else "已取消喜欢",
                         result.get("via") or "接口", name or song_id))

    jobs.submit("喜欢歌曲", work, on_done=done, on_error=lambda exc: st.set_error("操作失败：%s" % exc))


def track_from_item(item) -> dict:
    return item.as_dict()


# --------------------------------------------------------------------------
# 播放流程
# --------------------------------------------------------------------------


def play_track(context, track: dict, quality_override: str = ""):
    """获取直链 → 命中缓存就直接播，否则下载后再播（全程异步）。"""
    if not track or not track.get("id"):
        state(context).set_error("这首歌没有有效的 ID")
        return
    st = state(context)
    st.clear_error()
    st.current_name = track.get("name") or ""
    st.current_artists = track.get("artists") or ""
    st.current_album = track.get("album") or ""
    st.current_id = str(track.get("id"))
    st.duration = float(track.get("duration") or 0) / 1000.0
    st.position = 0.0
    if LYRIC["song_id"] != st.current_id:
        clear_lyric(st)          # 切歌就丢掉上一首的歌词时间轴
    st.current_liked = st.current_id in LIKED["ids"]
    st.is_playing = False
    st.is_paused = False
    PLAYBACK["loading"] = True
    PLAYBACK["manual_stop"] = False
    level = quality_override or quality(context)
    st.set_status("正在获取播放地址：%s - %s" % (st.current_name, st.current_artists))
    session = client(context)

    def fetch_url():
        return session.song_url(st.current_id, level)

    def after_url(info):
        st.current_source = "%s / %s / %s kbps" % (
            info.get("requested_level") or level, (info.get("type") or "?"), int((info.get("br") or 0) / 1000)
        )
        dest = player.cache_path(cache_dir(context), st.current_id, info.get("requested_level") or level, info.get("url") or "")
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            _start_playback(context, dest, st)
            return
        _download_and_play(context, info.get("url") or "", dest, st)

    jobs.submit("获取播放地址", fetch_url, on_done=after_url, on_error=lambda exc: _fail(context, exc))


def _download_and_play(context, url: str, dest: str, st):
    if not url:
        _fail(context, "接口没有返回可用的播放地址")
        return
    DOWNLOAD.update({"done": 0, "total": 0, "active": True, "name": os.path.basename(dest)})
    st.downloading = True
    st.download_progress = 0.0
    st.download_text = "正在下载：%s" % st.current_name
    st.set_status("正在下载音频…")

    def progress(done, total):
        DOWNLOAD["done"] = done
        DOWNLOAD["total"] = total

    def work():
        return player.download(url, dest, progress=progress)

    def done(path):
        DOWNLOAD["active"] = False
        st.downloading = False
        st.download_progress = 1.0
        st.download_text = ""
        enforce_cache_limit(context, path)
        _start_playback(context, path, st)

    def failed(exc):
        DOWNLOAD["active"] = False
        st.downloading = False
        st.download_text = ""
        _fail(context, exc)

    jobs.submit("下载音频", work, on_done=done, on_error=failed)


def _start_playback(context, path: str, st):
    if _ENGINE.play_file(path, expected_duration=st.duration):
        PLAYBACK["started"] = time.time()
        PLAYBACK["loading"] = False
        st.is_playing = True
        st.is_paused = False
        st.duration = _ENGINE.duration or st.duration
        st.position = 0.0
        st.set_status("正在播放：%s - %s" % (st.current_name, st.current_artists))
        st.clear_error()
        # 播放过的文件标记为“最近使用”，让缓存按 LRU 淘汰；顺便执行数量上限
        player.touch(path)
        enforce_cache_limit(context, path)
        if auto_lyric_enabled(context):
            load_lyric(context, st.current_id)
        update_lyric_position(st)
    else:
        PLAYBACK["loading"] = False
        _fail(context, _ENGINE.last_error or "播放失败")


def _fail(context, exc):
    st = state(context)
    PLAYBACK["loading"] = False
    st.is_playing = False
    st.is_paused = False
    st.set_error(exc)


def play_index(context, index: int, from_queue: bool = True):
    st = state(context)
    collection = st.queue if from_queue else st.tracks
    if not (0 <= index < len(collection)):
        st.set_error("列表里没有这一项")
        return
    if from_queue:
        st.queue_index = index
    play_track(context, collection[index].as_dict())


def play_all(context, shuffle: bool = False):
    """把当前显示的列表变成播放队列并从头播放（可选随机打乱）。"""
    st = state(context)
    if not len(st.tracks):
        st.set_error("当前列表是空的")
        return
    tracks = [item.as_dict() for item in st.tracks]
    if shuffle:
        import random
        random.shuffle(tracks)
    st.queue_clear()
    for track in tracks:
        st.queue_add(track)
    st.queue_index = 0
    apply_liked_flags(st)
    st.set_status("已把 %d 首加入播放队列" % len(tracks))
    play_track(context, tracks[0])


def enqueue_index(context, index: int, play_next: bool = False):
    st = state(context)
    if not (0 <= index < len(st.tracks)):
        st.set_error("请先在列表里选一首歌")
        return
    track = st.tracks[index].as_dict()
    had_items = len(st.queue) > 0
    st.queue_add(track)
    new_index = len(st.queue) - 1
    st.queue[new_index].liked = track["id"] in LIKED["ids"]
    if play_next and had_items:
        st.queue.move(new_index, min(st.queue_index + 1, new_index))
        st.set_status("已插入到下一首：%s" % track.get("name"))
    else:
        st.set_status("已加入队列：%s" % track.get("name"))


def next_track(context, step: int = 1, auto: bool = False) -> bool:
    """切歌；返回 True 表示成功切到下一首。"""
    st = state(context)
    if not len(st.queue):
        st.set_status("播放队列是空的")
        return False
    mode = st.loop_mode
    index = st.queue_index + step
    if index >= len(st.queue):
        if auto and mode == "all":
            index = 0
        else:
            _ENGINE.stop()
            st.is_playing = False
            st.is_paused = False
            st.set_status("队列已播放完")
            return False
    if index < 0:
        index = len(st.queue) - 1 if mode == "all" else 0
    st.queue_index = index
    play_track(context, st.queue[index].as_dict())
    return True


def replay_current(context):
    st = state(context)
    if len(st.queue) and 0 <= st.queue_index < len(st.queue):
        play_track(context, st.queue[st.queue_index].as_dict())


def toggle_pause(context):
    st = state(context)
    if not _ENGINE.is_active:
        if len(st.queue):
            play_track(context, st.queue[min(st.queue_index, len(st.queue) - 1)].as_dict())
        else:
            st.set_status("还没有正在播放的歌曲")
        return
    if _ENGINE.is_paused:
        _ENGINE.resume()
        st.is_paused = False
        st.is_playing = True
        st.set_status("继续播放")
    else:
        _ENGINE.pause()
        st.is_paused = True
        st.is_playing = False
        st.set_status("已暂停")


def stop_playback(context):
    st = state(context)
    PLAYBACK["manual_stop"] = True
    _ENGINE.stop()
    st.is_playing = False
    st.is_paused = False
    st.position = 0.0
    st.set_status("已停止")


# --------------------------------------------------------------------------
# 登录
# --------------------------------------------------------------------------


def apply_login(context, status: dict, cookie: str = ""):
    st = state(context)
    if cookie:
        save_cookie(cookie, context)
    st.logged_in = bool(status.get("logged_in"))
    st.nickname = status.get("nickname") or ""
    st.user_id = status.get("user_id") or ""
    vip = int(status.get("vip_type") or 0)
    st.vip_text = "黑胶 VIP" if vip else ("已登录" if st.logged_in else "未登录")
    if st.logged_in:
        st.account_message = "已登录：%s（ID %s）" % (st.nickname or "（无昵称）", st.user_id)
    else:
        st.account_message = "未登录（可以匿名使用「每日推荐」等公开内容）"
    # 登录后立刻同步「我喜欢的音乐」，退出则清掉本地 ♥ 标记
    if st.logged_in and LIKED["uid"] != st.user_id:
        refresh_liked(context, silent=True)
    elif not st.logged_in:
        clear_liked(st)
    else:
        apply_liked_flags(st)
    return st.logged_in


def verify_login(context, announce: bool = True):
    """在后台校验 Cookie 是否有效，并刷新账号信息。"""
    st = state(context)
    st.set_status("正在校验登录状态…")
    session = client(context)

    def work():
        return session.login_status()

    def done(status):
        apply_login(context, status, session.cookie)
        if announce:
            st.set_status("登录状态已刷新")

    jobs.submit("校验登录状态", work, on_done=done, on_error=lambda exc: st.set_error(exc))


def fetch_playlists(context):
    st = state(context)
    if not st.logged_in:
        st.set_error("请先登录后再拉取歌单")
        return
    st.set_status("正在获取歌单…")
    session = client(context)
    uid = st.user_id

    def work():
        return session.user_playlists(uid)

    def done(playlists):
        # 「我喜欢的音乐」永远排在最前面
        playlists.sort(key=lambda item: (0 if item.get("special_type") == 5 else 1, item.get("name") or ""))
        st.playlists_clear()
        for playlist in playlists:
            item = st.playlists.add()
            item.fill(playlist)
        st.set_status("已获取 %d 个歌单" % len(playlists))
        # 顺便同步「我喜欢的音乐」的歌曲 ID，这样列表里的 ♥ 标记和备用接口都能用
        refresh_liked(context, silent=True)

    jobs.submit("获取歌单", work, on_done=done, on_error=lambda exc: st.set_error(exc))


def open_playlist(context, playlist_id: str, title: str, load_all: bool = False):
    st = state(context)
    st.set_status("正在加载歌单：%s" % title)
    session = client(context)

    def work():
        detail = session.playlist_detail(playlist_id, track_limit=1000)
        tracks = detail.get("tracks") or []
        ids = detail.get("track_ids") or []
        missing = [sid for sid in ids if sid and sid not in {t.get("id") for t in tracks}]
        if missing and (load_all or len(tracks) < len(ids)):
            # 大歌单（例如「我喜欢的音乐」）只返回前若干首，这里按需补齐
            limit = len(missing) if load_all else 300
            tracks = tracks + session.songs_detail(missing[:limit])
        detail["tracks"] = tracks
        detail["missing"] = max(0, len(missing) - (0 if load_all else min(len(missing), 300)))
        return detail

    def done(detail):
        note = ""
        if detail.get("missing"):
            note = "还有 %d 首未加载，可点「补齐全部歌曲」" % detail["missing"]
        load_tracks_into_state(st, detail.get("tracks") or [], detail.get("name") or title, "playlist", note)

    jobs.submit("加载歌单", work, on_done=done, on_error=lambda exc: st.set_error(exc))


def load_daily(context):
    st = state(context)
    st.set_status("正在获取每日推荐…")
    session = client(context)

    def work():
        return session.daily_recommend()

    def done(tracks):
        note = "" if st.logged_in else "未登录时拿到的是通用推荐，登录后才是你的私人推荐"
        load_tracks_into_state(st, tracks, "每日推荐", "daily", note)
        st.tab = "recommend"

    jobs.submit("每日推荐", work, on_done=done, on_error=lambda exc: st.set_error(exc))


def load_radar(context):
    st = state(context)
    st.set_status("正在定位「私人雷达」…")
    session = client(context)
    manual = st.manual_radar_id or ""
    if not manual:
        settings = prefs(context)
        manual = getattr(settings, "radar_playlist_id", "") if settings else ""

    def work():
        if not session.profile:
            try:
                session.login_status()
            except Exception:  # noqa: BLE001
                pass
        radar = session.find_radar_playlist(manual)
        detail = session.playlist_detail(radar["id"], track_limit=1000)
        tracks = detail.get("tracks") or []
        ids = detail.get("track_ids") or []
        missing = [sid for sid in ids if sid and sid not in {t.get("id") for t in tracks}]
        if missing:
            tracks = tracks + session.songs_detail(missing[:300])
        radar["tracks"] = tracks
        radar["subscribed"] = detail.get("name")
        return radar

    def done(radar):
        st.radar_source = "来源：%s（歌单 %s）" % (radar.get("source") or "?", radar.get("id"))
        load_tracks_into_state(st, radar.get("tracks") or [], "私人雷达", "radar", st.radar_source)
        st.tab = "recommend"

    jobs.submit("私人雷达", work, on_done=done, on_error=lambda exc: st.set_error(exc))


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------


def tick():
    """定时器主体（主线程）。返回下次调用间隔（秒）。"""
    try:
        jobs.drain()
        st = state(bpy.context)

        # 下载进度
        if DOWNLOAD["active"]:
            total = DOWNLOAD["total"] or 0
            st.download_progress = (DOWNLOAD["done"] / total) if total else 0.0
            st.download_text = "正在下载：%s  %s" % (
                st.current_name, player.human_size(DOWNLOAD["done"]) + ((" / " + player.human_size(total)) if total else "")
            )

        # 跳转请求
        target = props.take_seek()
        if target is not None and _ENGINE.is_active:
            _ENGINE.seek(target)

        # 音量跟随面板
        if abs(_ENGINE.volume - st.volume) > 0.001:
            _ENGINE.set_volume(st.volume)

        # 播放进度与结束检测
        if _ENGINE.is_active:
            st.position = _ENGINE.position
            duration = _ENGINE.duration
            if duration > 0:
                st.duration = duration
            st.is_playing = _ENGINE.is_playing
            st.is_paused = _ENGINE.is_paused
            if st.is_playing:
                props.sync_seek(st, st.position)

        # 结束检测必须独立于 is_active：声音播完后 handle 仍在，但 status 已经变成 False，
        # 如果放进上面的分支里，就永远等不到“自动下一首”。
        if not PLAYBACK["loading"] and _is_finished(st):
            _handle_track_end(bpy.context)
        elif not _ENGINE.is_active and st.is_playing:
            st.is_playing = False

        # 歌词当前行（浮层与面板共用；没有时间轴时什么都不做）
        if LYRIC["timeline"]:
            update_lyric_position(st)

        # 缓存信息（每秒刷新一次就够）
        now = time.time()
        if now - PLAYBACK.get("cache_stamp", 0) > 3.0:
            PLAYBACK["cache_stamp"] = now
            refresh_cache_text(st)

        # 有后台任务时保证界面刷新
        if jobs.busy() or _ENGINE.is_active or st.lyric_overlay:
            redraw()
    except Exception:  # noqa: BLE001 - 定时器里绝不能抛异常
        import traceback
        utils.log("定时器异常：\n%s" % traceback.format_exc())
    return 0.5


def _is_finished(st) -> bool:
    if _ENGINE.finished:
        return True
    if _ENGINE.duration > 0:
        return False
    # 少数格式拿不到长度，用元数据时长兜底
    started = PLAYBACK.get("started") or 0.0
    if st.is_paused or not started or st.duration <= 1.0:
        return False
    return (time.time() - started) >= st.duration + 1.5


def _handle_track_end(context):
    st = state(context)
    if PLAYBACK["manual_stop"]:
        PLAYBACK["manual_stop"] = False
        return
    if time.time() - PLAYBACK.get("last_auto_next", 0) < 1.0:
        return
    PLAYBACK["last_auto_next"] = time.time()
    if st.loop_mode == "one":
        replay_current(context)
        return
    if not next_track(context, 1, auto=True):
        st.is_playing = False


def refresh_cache_text(st=None):
    st = st or state(bpy.context)
    count, size = player.cache_size(cache_dir(bpy.context))
    st.cache_text = "%d 个文件 / %s" % (count, player.human_size(size))
