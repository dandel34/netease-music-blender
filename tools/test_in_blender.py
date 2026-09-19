# -*- coding: utf-8 -*-
"""在真实 Blender 里跑一遍插件：注册、加密、网络、下载、aud 播放、面板绘制冒烟。

    blender --background --factory-startup --python tools/test_in_blender.py

结果写到 .tmp/blender_report.json，并打印摘要。
"""

import json
import math
import os
import shutil
import struct
import sys
import time
import traceback
import wave

import bpy

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
TMP = os.path.join(ROOT, ".tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, ROOT)

REPORT = {}


def step(name, fn):
    start = time.time()
    try:
        value = fn()
        REPORT[name] = value
        print("[OK]   %-34s %.2fs" % (name, time.time() - start))
        return value
    except Exception as exc:  # noqa: BLE001
        REPORT[name] = {"error": "%s: %s" % (type(exc).__name__, exc),
                        "traceback": traceback.format_exc()[-1200:]}
        print("[FAIL] %-34s %s" % (name, exc))
        return None


class FakeLayout:
    """记录调用、可无限链式的假 layout，用来在无界面环境下跑一遍 draw()。"""

    def __init__(self, name="layout"):
        self.name = name
        self.calls = []

    def __getattr__(self, item):
        def method(*args, **kwargs):
            self.calls.append(item)
            return FakeLayout(item)
        return method


# --------------------------------------------------------------------------
# 1. 注册
# --------------------------------------------------------------------------


def test_import_register():
    """按用户真实路径启用插件：这样 Blender 才会创建偏好对象（runtime.prefs() 才有东西）。"""
    import netease_music
    enabled_via = ""
    try:
        result = bpy.ops.preferences.addon_enable(module="netease_music")
        enabled_via = "addon_enable %s" % list(result)
    except Exception as exc:  # noqa: BLE001
        netease_music.register()
        enabled_via = "找不到插件路径，退回直接 register()：%s" % exc
    return {
        "version": netease_music.__version__,
        "enabled_via": enabled_via,
        "has_state": hasattr(bpy.types.Scene, "netease"),
        "has_prefs": bpy.context.preferences.addons.get("netease_music") is not None,
        "operators": sorted(n for n in dir(bpy.ops.netease) if not n.startswith("_")),
        "panels": [c for c in ("NM_PT_main", "NM_PT_selftest") if hasattr(bpy.types, c)],
        "uils": [c for c in ("NM_UL_tracks", "NM_UL_playlists") if hasattr(bpy.types, c)],
        "timer": bpy.app.timers.is_registered(netease_music._tick),
    }


def test_state_defaults():
    st = bpy.context.scene.netease
    st.volume = 0.5
    st.tab = "player"
    st.tracks_add({"id": "1", "name": "测试歌曲", "artists": "歌手", "album": "专辑",
                   "duration": 215000, "fee": 0, "mv_id": "0"})
    st.playlists.add().fill({"id": "2", "name": "我喜欢的音乐", "track_count": 12, "special_type": 5})
    st.queue_add({"id": "1", "name": "测试歌曲", "artists": "歌手", "duration": 215000})
    return {
        "track_name": st.tracks[0].name,
        "duration_text": st.tracks[0].duration_text,
        "playlist_special": st.playlists[0].special_type,
        "queue_len": len(st.queue),
        "seek_cb_guarded": _seek_guard_test(st),
    }


def _seek_guard_test(st):
    """定时器回写进度不应触发跳转请求。"""
    from netease_music import props
    props.take_seek()
    props.sync_seek(st, 12.5)
    guarded = props.take_seek() is None
    st.seek_seconds = 30.0  # 用户拖动 → 应产生请求
    requested = props.take_seek()
    return {"no_request_on_sync": guarded, "user_request": requested}


def test_crypto():
    from netease_music import crypto
    results = crypto.selftest()
    return {"results": {k: v[0] for k, v in results.items()},
            "failed": [k for k, v in results.items() if not v[0]],
            "modulus_hex_len": len(crypto.WEAPI_MODULUS)}


def test_jobs_pipeline():
    from netease_music import jobs
    flag = {"done": False, "value": None}

    def work():
        time.sleep(0.3)
        return 42

    jobs.submit("测试任务", work, on_done=lambda value: flag.update({"done": True, "value": value}))
    deadline = time.time() + 8
    while time.time() < deadline and not flag["done"]:
        time.sleep(0.1)
        jobs.drain()
    return flag


# --------------------------------------------------------------------------
# 2. 网络
# --------------------------------------------------------------------------


def test_network():
    from netease_music import runtime
    session = runtime.client()
    status = session.login_status()
    daily = session.daily_recommend()
    url = session.song_url("33894312", "exhigh") if not daily else session.song_url(daily[0]["id"], "exhigh")
    return {
        "logged_in": status["logged_in"],
        "daily_count": len(daily),
        "first_daily": ("%s - %s" % (daily[0]["name"], daily[0]["artists"])) if daily else None,
        "url_ok": bool(url.get("url")),
        "url_type": url.get("type"),
        "url_br": url.get("br"),
        "transports_used": sorted({entry[0] for entry in session.transport_log}),
    }


def test_download_and_play():
    from netease_music import player, runtime, utils
    session = runtime.client()
    cache = runtime.cache_dir()
    utils.ensure_dir(cache)
    result = {}
    for level, song in (("exhigh", "33894312"), ("standard", "33894312")):
        info = session.song_url(song, level)
        dest = player.cache_path(cache, song, info.get("requested_level") or level, info.get("url") or "")
        if os.path.exists(dest):          # 强制重新下载，顺便验证进度回调
            os.remove(dest)
        seen = {"bytes": 0, "total": 0}
        player.download(info["url"], dest,
                        progress=lambda d, t: seen.update({"bytes": d, "total": t}))
        engine = runtime.engine()
        played = engine.play_file(dest, expected_duration=0.0)
        time.sleep(1.2)
        first = engine.position
        time.sleep(1.0)
        second = engine.position
        duration = engine.duration
        paused_ok = engine.pause() and engine.is_paused
        engine.resume()
        time.sleep(0.4)
        engine.seek(max(0.0, duration / 2.0))
        time.sleep(0.4)
        after_seek = engine.position
        engine.set_volume(0.3)
        engine.stop()
        result[level] = {
            "file": os.path.basename(dest), "bytes": os.path.getsize(dest),
            "progress_bytes": seen["bytes"], "total": seen["total"],
            "play_called": played, "position_advanced": round(second - first, 3),
            "engine_duration": round(duration, 2), "pause_ok": paused_ok,
            "seek_position": round(after_seek, 2), "last_error": engine.last_error,
        }
    return result


def test_tone_playback():
    """本地生成的 WAV：验证 aud 对音频文件的解码与进度。"""
    from netease_music import runtime
    path = os.path.join(TMP, "tone.wav")
    rate = 44100
    with wave.open(path, "w") as fh:
        fh.setnchannels(2)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * 1.5)):
            value = int(12000 * math.sin(2 * math.pi * 440 * i / rate))
            frames += struct.pack("<hh", value, value)
        fh.writeframes(bytes(frames))
    engine = runtime.engine()
    ok = engine.play_file(path)
    time.sleep(0.8)
    pos = engine.position
    dur = engine.duration
    engine.stop()
    return {"ok": ok, "position": round(pos, 3), "duration": round(dur, 3)}


# --------------------------------------------------------------------------
# 3. 面板绘制冒烟测试
# --------------------------------------------------------------------------


def test_panel_draw():
    from netease_music import ui
    st = bpy.context.scene.netease

    class Stub:
        pass

    stub = Stub()
    stub.layout = FakeLayout()
    for name in ("_header", "_account", "_player", "_playlists", "_recommend",
                 "_track_list", "_lyric_overlay", "_lyric_text", "_footer"):
        setattr(stub, name, getattr(ui.NM_PT_main, name).__get__(stub))

    # 覆盖各种分支：未登录 / 已登录 / 已喜欢 / 播放中 / 歌词浮层 / 下载中 / 有错误
    scenarios = {
        "未登录": {"logged_in": False},
        "已登录": {"logged_in": True, "nickname": "测试用户", "user_id": "123", "vip_text": "黑胶 VIP",
                   "liked_text": "已同步 88 首", "liked_count": 88},
        "已喜欢当前歌曲": {"current_liked": True, "current_id": "1", "current_name": "歌"},
        "播放中": {"is_playing": True, "current_id": "1", "current_name": "歌", "current_artists": "手",
                   "current_source": "exhigh / m4a", "duration": 200.0, "position": 30.0,
                   "show_lyric": True, "lyric": "[00:01.00] 第一行\n[00:02.00] 第二行",
                   "downloading": True, "download_progress": 0.4, "download_text": "正在下载"},
        "歌词浮层开启": {"lyric_overlay": True, "lyric_count": 42, "lyric_line": "当前这一句",
                         "lyric_translation": "translation", "lyric_next": "下一句",
                         "current_id": "1", "current_name": "歌"},
        "出错": {"error_text": "第一行错误\n第二行错误", "status_text": "出错了"},
    }
    drawn = {}
    for key, values in scenarios.items():
        for field, value in values.items():
            setattr(st, field, value)
        err = None
        try:
            for tab in ("account", "player", "playlists", "recommend"):
                st.tab = tab
                ui.NM_PT_main.draw(stub, bpy.context)
        except Exception as exc:  # noqa: BLE001
            err = "%s: %s\n%s" % (type(exc).__name__, exc, traceback.format_exc()[-600:])
        drawn[key] = err or "ok"
        # 复位
        for field in values:
            if isinstance(getattr(st, field), bool):
                setattr(st, field, False)
    # 列表行绘制（UIList 不能被实例化，用 __get__ 绑定到桩对象上）
    list_errors = {}

    class StubList:
        pass

    ul = StubList()
    for propname, active in (("tracks", "track_index"), ("queue", "queue_index")):
        try:
            for liked in (False, True):
                st.tracks[0].liked = liked
                st.queue_add(st.tracks[0].as_dict()).liked = liked
                ui.NM_UL_tracks.draw_item.__get__(ul)(None, FakeLayout(), st, st.tracks[0],
                                                      "PLAY", st, active, 0)
                ui.NM_UL_tracks.draw_item.__get__(ul)(None, FakeLayout(), st, st.queue[0],
                                                      "PLAY", st, "queue_index", 0)
            ui.NM_UL_playlists.draw_item.__get__(ul)(None, FakeLayout(), st, st.playlists[0],
                                                     "FILE_FOLDER", st, "playlist_index", 0)
            list_errors[propname] = "ok"
        except Exception as exc:  # noqa: BLE001
            list_errors[propname] = "%s: %s" % (type(exc).__name__, exc)
    st.queue_clear()
    return {"scenarios": drawn, "lists": list_errors, "calls": len(stub.layout.calls)}


def test_login_surface():
    """二维码登录已移除：确认相关属性、操作符都不存在了。"""
    from netease_music import api
    st = bpy.context.scene.netease
    # 注意：bpy.ops.xxx 对任意属性名都会返回操作符包装对象，必须看真实注册表 dir()
    registered = {name for name in dir(bpy.ops.netease) if not name.startswith("_")}
    gone_props = [name for name in ("qr_status", "qr_key", "qr_pending", "qr_image_path", "qr_url",
                                     "phone", "password", "captcha", "country_code")
                  if name in st.bl_rna.properties]
    gone_ops = [name for name in ("login_qr", "cancel_qr", "open_qr", "login_phone", "send_captcha")
                if name in registered]
    gone_api = [name for name in ("qr_key", "qr_create", "qr_check", "login_cellphone", "captcha_sent")
                if hasattr(api.NeteaseClient, name)]
    return {"registered_count": len(registered),
            "state_props_left": gone_props, "operators_left": gone_ops, "api_methods_left": gone_api,
            "cookie_input_kept": "cookie_input" in st.bl_rna.properties,
            "login_ops_kept": sorted(n for n in ("login_cookie", "paste_cookie", "open_web_login",
                                                 "logout", "refresh_account") if n in registered)}


def test_like_flow():
    """「加入我喜欢的音乐」：匿名时给出可读错误；接口可达（301 需要登录）；♥ 标记同步。"""
    from netease_music import api, runtime
    st = bpy.context.scene.netease
    result = {"operators": sorted(name for name in ("like_track", "like_current", "refresh_liked")
                                  if name in dir(bpy.ops.netease))}

    # 1) 未登录：应给友好提示而不是抛异常
    st.logged_in = False
    st.clear_error()
    runtime.like_track(bpy.context, "347230", "测试歌曲", like=True)
    result["anonymous_message"] = st.error_text[:60]

    # 2) 接口可达性：匿名叫点赞会返回“需要登录”，而不是“接口未找到”
    session = runtime.client()
    try:
        session.set_like("347230", True)
        result["anonymous_api"] = "竟然成功了（未预期）"
    except api.ApiError as exc:
        result["anonymous_api"] = {"need_login": exc.need_login, "code": exc.code, "msg": str(exc)[:60]}
    except Exception as exc:  # noqa: BLE001
        result["anonymous_api"] = "%s: %s" % (type(exc).__name__, exc)

    # 3) ♥ 标记：给列表/队列/当前歌曲打标
    st.tracks_clear()
    st.tracks_add({"id": "1", "name": "第一首", "artists": "A", "duration": 1000})
    st.tracks_add({"id": "2", "name": "第二首", "artists": "B", "duration": 2000})
    st.queue_clear()
    st.queue_add({"id": "2", "name": "第二首", "artists": "B", "duration": 2000})
    st.current_id = "2"
    runtime.LIKED.update({"ids": {"2"}, "loaded": True, "uid": "999"})
    runtime.apply_liked_flags(st)
    result["flags"] = {
        "track1": st.tracks[0].liked, "track2": st.tracks[1].liked,
        "queued": st.queue[0].liked, "current": st.current_liked,
        "text": st.liked_text, "count": st.liked_count,
    }

    # 4) 登录后 load_tracks 会自动带上 ♥
    runtime.load_tracks_into_state(st, [{"id": "2", "name": "第二首", "artists": "B", "duration": 2000},
                                        {"id": "3", "name": "第三首", "artists": "C", "duration": 3000}],
                                   "测试列表", "playlist")
    result["reload_flags"] = [item.liked for item in st.tracks]

    # 5) 退出登录要清掉本地标记
    st.logged_in = False
    runtime.clear_liked(st)
    result["after_logout"] = [item.liked for item in st.tracks]
    runtime.LIKED.update({"ids": set(), "loaded": False, "uid": ""})
    return result


def test_playlist_flow():
    """完整走一遍「加载歌单 → 写进列表」的异步流程。"""
    from netease_music import runtime
    st = bpy.context.scene.netease
    st.tracks_clear()
    runtime.open_playlist(bpy.context, "3778678", "热歌榜")
    deadline = time.time() + 25
    while time.time() < deadline:
        time.sleep(0.2)
        runtime.tick()
        if st.list_kind == "playlist" and len(st.tracks) > 0:
            break
    return {"tracks": len(st.tracks), "title": st.list_title, "kind": st.list_kind,
            "note": st.list_note[:40], "first": st.tracks[0].name if len(st.tracks) else None,
            "status": st.status_text}


def test_daily_flow():
    from netease_music import runtime
    st = bpy.context.scene.netease
    runtime.load_daily(bpy.context)
    deadline = time.time() + 25
    while time.time() < deadline:
        time.sleep(0.2)
        runtime.tick()
        if st.list_kind == "daily" and len(st.tracks) > 0:
            break
    return {"tracks": len(st.tracks), "title": st.list_title, "kind": st.list_kind,
            "first": st.tracks[0].name if len(st.tracks) else None}


def test_autonext():
    """第一首放一段 1.5 秒的本地音频，验证播完自动切到下一首（含取直链+下载）。"""
    from netease_music import runtime
    st = bpy.context.scene.netease
    st.queue_clear()
    st.queue_add({"id": "local", "name": "本地铃声", "artists": "测试", "duration": 1500})
    st.queue_add({"id": "33894312", "name": "真实歌曲", "artists": "测试", "duration": 267000})
    st.queue_index = 0
    st.loop_mode = "off"
    runtime.PLAYBACK["manual_stop"] = False
    runtime.PLAYBACK["last_auto_next"] = 0.0
    tone = os.path.join(TMP, "tone.wav")
    engine = runtime.engine()
    engine.play_file(tone, expected_duration=1.5)
    runtime.PLAYBACK["started"] = time.time()
    # 等它自然播完，再让定时器处理“下一首”
    deadline = time.time() + 20
    switched = False
    while time.time() < deadline:
        time.sleep(0.3)
        runtime.tick()
        if st.queue_index == 1 and st.current_id == "33894312" and engine.is_active:
            switched = True
            break
    result = {"switched": switched, "queue_index": st.queue_index, "current": st.current_name,
              "engine_active": engine.is_active, "error": st.error_text[:80]}
    engine.stop()
    return result


def test_cookie_helpers():
    from netease_music import api
    jar = api.NeteaseClient.parse_cookie_string("MUSIC_U=abc; __csrf=xyz;\n os=pc")
    client = api.NeteaseClient(cookie="MUSIC_U=old")
    client.merge_set_cookie({"set-cookie": "MUSIC_U=new; Path=/; HttpOnly, NMTID=123; Path=/"})
    return {"parsed": jar, "merged": api.NeteaseClient.parse_cookie_string(client.cookie)}


def test_operators():
    st = bpy.context.scene.netease
    st.tracks_clear()
    for index in range(3):
        st.tracks_add({"id": str(index + 1), "name": "歌曲 %d" % index, "artists": "歌手",
                       "duration": 180000, "fee": 0, "mv_id": "0"})
    st.track_index = 1
    bpy.ops.netease.enqueue(index=-1, play_next=False)
    queued = len(st.queue)
    st.queue_index = 0
    bpy.ops.netease.queue_remove(index=0)
    after_remove = len(st.queue)
    bpy.ops.netease.queue_clear()
    cleared = len(st.queue)
    bpy.ops.netease.stop()
    return {"enqueued": queued, "after_remove": after_remove, "after_clear": cleared,
            "status": st.status_text}


def test_runtime_tick():
    """手动跑一次 tick：确认定时器主体不抛异常，并能回收任务。"""
    from netease_music import jobs, runtime
    st = bpy.context.scene.netease
    done = {"n": 0}
    jobs.submit("tick 测试", lambda: (time.sleep(0.2), "ok")[1], on_done=lambda v: done.update({"n": done["n"] + 1}))
    time.sleep(0.4)
    interval = runtime.tick()
    return {"interval": interval, "callbacks": done["n"], "status": st.status_text}


def test_reregister():
    """注销要干净（属性/面板/定时器/浮层/快捷键都摘掉），而且能重新启用。"""
    import netease_music
    from netease_music import overlay
    try:
        bpy.ops.preferences.addon_disable(module="netease_music")
        disabled_via = "addon_disable"
    except Exception:  # noqa: BLE001
        netease_music.unregister()
        disabled_via = "直接 unregister()"
    unregistered = {
        "state_removed": not hasattr(bpy.types.Scene, "netease"),
        "panel_removed": not hasattr(bpy.types, "NM_PT_main"),
        "timer_removed": not bpy.app.timers.is_registered(netease_music._tick),
        "overlay_removed": not overlay.is_registered(),
        "keymap_removed": not overlay.keymap_registered(),
    }
    try:
        bpy.ops.preferences.addon_enable(module="netease_music")
        enabled_via = "addon_enable"
    except Exception:  # noqa: BLE001
        netease_music.register()
        enabled_via = "直接 register()"
    return {"disabled_via": disabled_via, "enabled_via": enabled_via, "after_unregister": unregistered,
            "state_back": hasattr(bpy.types.Scene, "netease"),
            "timer_back": bpy.app.timers.is_registered(netease_music._tick),
            "overlay_back": overlay.is_registered()}


# --------------------------------------------------------------------------
# 新功能：动态歌词浮层 + 缓存上限
# --------------------------------------------------------------------------


def test_lyric_overlay():
    """歌词时间轴 + 浮层布局（纯计算，不碰 GPU）。"""
    from netease_music import overlay, runtime
    st = bpy.context.scene.netease
    settings = runtime.prefs()
    result = {}

    runtime.set_lyric(st, {
        "lrc": "[00:01.00]第一句\n[00:05.00]第二句\n[00:09.00]第三句\n[00:13.00]第四句",
        "translated": "[00:05.00]translated second line",
        "merged": "（面板用的整段文本）",
    }, "song-1")
    st.current_name = "测试歌曲"
    st.current_artists = "测试歌手"
    st.duration = 200.0
    st.position = 6.0
    runtime.update_lyric_position(st)
    settings.lyric_line_count = "3"          # 多行模式：验证上下文都在
    settings.lyric_show_title = True
    settings.lyric_show_translation = True

    result["timeline_lines"] = st.lyric_count
    result["current_line"] = st.lyric_line
    result["translation"] = st.lyric_translation
    result["next_line"] = st.lyric_next
    result["index"] = st.lyric_index
    result["panel_text"] = st.lyric[:12]
    result["line_counts_3"] = list(overlay.line_counts(settings))

    window = runtime.lyric_window(st, *overlay.line_counts(settings))
    result["window_offsets"] = [offset for offset, _item in window["items"]]

    scene = overlay.build_scene(1280, 720, st, settings, window)
    x, y, w, h = scene["box"]
    result["box"] = [round(v, 1) for v in scene["box"]]
    result["inside"] = x >= 0 and y >= 0 and x + w <= 1280 and y + h <= 720
    result["rects"] = len(scene["rects"])
    result["texts"] = len(scene["texts"])
    result["has_current"] = any(item.get("current") and item["text"] == "第二句" for item in scene["texts"])
    result["has_translation"] = any(item["text"] == "translated second line" for item in scene["texts"])
    result["has_title"] = any("测试歌曲" in item["text"] for item in scene["texts"])
    result["font_size"] = scene["font_size"]

    st.lyric_drag_active = True
    dragged = overlay.build_scene(1280, 720, st, settings, window)
    result["drag_border_added"] = len(dragged["rects"]) - len(scene["rects"])
    st.lyric_drag_active = False

    # 小视口：不能越界，且字号会自动缩小
    small = overlay.build_scene(240, 150, st, settings, window)
    sx, sy, sw, sh = small["box"]
    result["small_inside"] = sx >= 0 and sy >= 0 and sx + sw <= 240 and sy + sh <= 150
    result["small_font"] = small["font_size"]

    result["clamp"] = [overlay.clamp_position(v) for v in (-1.0, 0.5, 2.0, "bad")]

    runtime.clear_lyric(st)
    result["cleared"] = (st.lyric_line, st.lyric_count, st.lyric_index)
    settings.lyric_line_count = "1"
    st.position = 0.0                     # 复位，避免影响后续步骤
    return result


def test_lyric_lines_and_instrumental():
    """默认 1 行（当前句 + 翻译）；纯音乐/无歌词时只显示歌名与歌手。"""
    from netease_music import overlay, runtime
    st = bpy.context.scene.netease
    settings = runtime.prefs()
    result = {}

    old_count = settings.lyric_line_count
    old_title = settings.lyric_show_title
    old_translation = settings.lyric_show_translation
    try:
        settings.lyric_show_title = True
        settings.lyric_show_translation = True
        result["counts_by_preset"] = {value: list(lyrics_counts(value)) for value in ("1", "3", "5", "7")}

        runtime.set_lyric(st, {
            "lrc": "[00:01.00]第一句\n[00:05.00]第二句\n[00:09.00]第三句\n[00:13.00]第四句",
            "translated": "[00:05.00]translated second",
            "merged": "整段",
        }, "song-1")
        st.current_name = "测试歌曲"
        st.current_artists = "测试歌手"
        st.position = 6.0
        runtime.update_lyric_position(st)

        # 默认：只画当前句 + 翻译
        settings.lyric_line_count = "1"
        one = overlay.build_scene(1000, 600, st, settings, runtime.lyric_window(st, *overlay.line_counts(settings)))
        lyric_texts = [item["text"] for item in one["texts"] if not item.get("title")]
        result["one_line_texts"] = lyric_texts
        result["one_line_has_title"] = any(item.get("title") for item in one["texts"])
        result["one_line_no_next"] = not any("第三句" in text for text in lyric_texts)
        result["one_line_translation"] = "translated second" in lyric_texts
        result["one_line_box_h"] = round(one["box"][3], 1)

        # 3 行：上下文都出来
        settings.lyric_line_count = "3"
        three = overlay.build_scene(1000, 600, st, settings, runtime.lyric_window(st, *overlay.line_counts(settings)))
        three_texts = [item["text"] for item in three["texts"] if not item.get("title")]
        result["three_line_texts"] = three_texts
        result["three_line_box_h"] = round(three["box"][3], 1)
        result["three_taller_than_one"] = three["box"][3] > one["box"][3]

        # 纯音乐 / 无歌词：只显示歌名与歌手
        runtime.clear_lyric(st)
        instrumental = overlay.build_scene(1000, 600, st, settings, {"index": -1, "items": []})
        result["instrumental_texts"] = [item["text"] for item in instrumental["texts"]]
        result["instrumental_title_only"] = (
            len(instrumental["texts"]) == 1
            and instrumental["texts"][0]["text"] == "测试歌曲 - 测试歌手"
            and instrumental["texts"][0].get("title")
        )
        result["instrumental_no_placeholder"] = not any(
            "暂无歌词" in item["text"] or "纯音乐" in item["text"] for item in instrumental["texts"]
        )
        result["instrumental_size_is_main"] = instrumental["texts"][0]["size"] == settings.lyric_font_size
        result["has_lyric_flag"] = [one["has_lyric"], instrumental["has_lyric"]]

        # 没在播放时给个占位，不至于空一块
        st.current_name = ""
        st.current_artists = ""
        idle = overlay.build_scene(1000, 600, st, settings, {"index": -1, "items": []})
        result["idle_texts"] = [item["text"] for item in idle["texts"]]
    finally:
        settings.lyric_line_count = old_count
        settings.lyric_show_title = old_title
        settings.lyric_show_translation = old_translation
    return result


def lyrics_counts(value):
    from netease_music import lyrics
    return lyrics.counts_for(value)


def test_icon_names():
    """源码里用到的图标必须都存在于 Blender 图标表（否则面板一打开就报错）。"""
    import re
    icons = set()
    for function in bpy.types.UILayout.bl_rna.functions:
        if function.identifier == "label":
            for param in function.parameters:
                if param.identifier == "icon":
                    icons = {item.identifier for item in param.enum_items}
    used = {}
    root = os.path.join(ROOT, "netease_music")
    for name in sorted(os.listdir(root)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(root, name), encoding="utf-8") as fh:
            text = fh.read()
        for icon in re.findall(r'icon="([A-Z0-9_]+)"', text):
            used.setdefault(icon, set()).add(name)
    missing = {icon: sorted(files) for icon, files in used.items() if icon not in icons}
    return {"available": len(icons), "used": len(used), "missing": missing}


def test_cjk_text():
    """中文歌词要能画出来：默认字体必须带 CJK 回退（用度量间接验证）。"""
    import blf
    blf.size(0, 24)
    cjk = blf.dimensions(0, "中文歌词")
    mixed = blf.dimensions(0, "海阔天空 Beyond")
    ascii_only = blf.dimensions(0, "Beyond")
    bundled = os.path.join(os.path.dirname(bpy.app.binary_path), "5.2",
                           "datafiles", "fonts", "Noto Sans CJK Regular.woff2")
    custom = None
    if os.path.exists(bundled):
        custom = blf.load(bundled)
    return {
        "cjk_width": round(cjk[0], 1), "cjk_height": round(cjk[1], 1),
        "mixed_width": round(mixed[0], 1), "ascii_width": round(ascii_only[0], 1),
        "cjk_has_size": cjk[0] > 40 and mixed[0] > ascii_only[0],
        "bundled_font": os.path.basename(bundled) if os.path.exists(bundled) else None,
        "custom_font_id": custom,
    }


def test_cache_runtime():
    """按偏好设置里的上限自动清理缓存（用真实函数 + 临时目录）。"""
    import pathlib

    from netease_music import player, runtime
    st = bpy.context.scene.netease
    settings = runtime.prefs()
    cache = os.path.join(TMP, "cache_case")
    if os.path.isdir(cache):
        shutil.rmtree(cache, ignore_errors=True)
    os.makedirs(cache, exist_ok=True)
    paths = []
    for index in range(6):
        path = os.path.join(cache, "song%d.mp3" % index)
        pathlib.Path(path).write_bytes(b"a" * (10 * (index + 1)))
        os.utime(path, (2000 + index, 2000 + index))
        paths.append(path)

    old_dir, old_limit, old_enabled = settings.cache_dir, settings.cache_limit_count, settings.cache_limit_enabled
    result = {}
    try:
        settings.cache_dir = cache
        settings.cache_limit_enabled = True
        settings.cache_limit_count = 3
        runtime.enforce_cache_limit(bpy.context, keep=paths[5])     # 第 6 首视为“正在播放”
        left = sorted(os.path.basename(item[0]) for item in player.cache_files(cache))
        result["kept"] = left
        result["playing_kept"] = "song5.mp3" in left
        result["count_within_limit_plus_playing"] = len(left) == 4
        result["cache_text"] = st.cache_text

        runtime.enforce_cache_limit(bpy.context, keep=paths[5])
        result["idempotent"] = sorted(os.path.basename(i[0]) for i in player.cache_files(cache)) == left

        settings.cache_limit_enabled = False
        for index in range(4):
            pathlib.Path(os.path.join(cache, "extra%d.mp3" % index)).write_bytes(b"b")
        runtime.enforce_cache_limit(bpy.context)
        result["disabled_does_nothing"] = len(player.cache_files(cache)) == len(left) + 4
    finally:
        settings.cache_dir = old_dir
        settings.cache_limit_count = old_limit
        settings.cache_limit_enabled = old_enabled
        shutil.rmtree(cache, ignore_errors=True)
    return result


def test_overlay_registration():
    from netease_music import overlay
    was_handler = overlay.is_registered()
    was_keymap = overlay.keymap_registered()
    overlay.unregister_handler()
    removed = not overlay.is_registered()
    overlay.register_handler()
    back = overlay.is_registered()
    # 快捷键：后台模式没有 addon 键位配置，函数必须安全返回
    overlay.unregister_keymap()
    overlay.refresh_keymap(True)
    keymap_after = overlay.keymap_registered()
    overlay.refresh_keymap(False)
    overlay.refresh_keymap(was_keymap)
    return {"handler_was": was_handler, "handler_removed": removed, "handler_back": back,
            "keymap_was": was_keymap, "keymap_after_enable": keymap_after,
            "draw_error": overlay.draw_error()[:80],
            "handler_registered_now": overlay.is_registered()}


def test_new_surface():
    """新功能对外的接口都在（属性、操作符、版本号）。"""
    import netease_music
    from netease_music import overlay, runtime
    st = bpy.context.scene.netease
    settings = runtime.prefs()
    props = [name for name in ("lyric_overlay", "lyric_pos_x", "lyric_pos_y", "lyric_drag_active",
                               "lyric_line", "lyric_translation", "lyric_next", "lyric_index",
                               "lyric_count") if name in st.bl_rna.properties]
    settings_props = [name for name in ("cache_limit_enabled", "cache_limit_count", "auto_load_lyric",
                                        "lyric_font_size", "lyric_bg_opacity", "lyric_show_translation",
                                        "lyric_show_title", "lyric_font_path", "lyric_hotkey")
                      if name in settings.bl_rna.properties]
    registered = {name for name in dir(bpy.ops.netease) if not name.startswith("_")}
    operators = [name for name in ("toggle_lyric_overlay", "drag_lyric", "reset_lyric_pos", "trim_cache")
                 if name in registered]
    return {"version": netease_music.__version__,
            "state_props": props, "pref_props": settings_props, "operators": operators,
            "operator_count": len(registered),
            "has_lyrics_module": bool(runtime.lyric_timeline() is not None),
            "overlay_module": hasattr(overlay, "build_scene")}


# --------------------------------------------------------------------------


def main():
    step("1 导入与注册", test_import_register)
    step("2 属性状态", test_state_defaults)
    step("3 加密测试向量", test_crypto)
    step("4 后台任务管道", test_jobs_pipeline)
    step("5 真实接口（匿名）", test_network)
    step("6 下载并播放（网易云）", test_download_and_play)
    step("7 本地 WAV 播放", test_tone_playback)
    step("8 面板绘制冒烟", test_panel_draw)
    step("9 登录方式只剩 Cookie", test_login_surface)
    step("10 操作符", test_operators)
    step("11 定时器主体", test_runtime_tick)
    step("12 歌单加载流程", test_playlist_flow)
    step("13 每日推荐流程", test_daily_flow)
    step("14 自动下一首", test_autonext)
    step("15 Cookie 解析", test_cookie_helpers)
    step("16 加入我喜欢的音乐", test_like_flow)
    step("17 动态歌词浮层布局", test_lyric_overlay)
    step("18 歌词行数与纯音乐显示", test_lyric_lines_and_instrumental)
    step("19 图标名校验", test_icon_names)
    step("20 中文字体度量", test_cjk_text)
    step("21 缓存上限（运行时）", test_cache_runtime)
    step("22 浮层注册与快捷键", test_overlay_registration)
    step("23 新接口清单", test_new_surface)
    step("24 注销并重注册", test_reregister)

    out = os.path.join(TMP, "blender_report.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(REPORT, fh, ensure_ascii=False, indent=1)
    print("REPORT_PATH", out)
    failed = [name for name, value in REPORT.items() if isinstance(value, dict) and value.get("error")]
    print("FAILED_STEPS", failed)
    print(json.dumps(REPORT, ensure_ascii=False, indent=1)[:3000])


main()
