# -*- coding: utf-8 -*-
"""离线单元测试：数据标准化、私人雷达定位逻辑、Cookie 处理、协议降级。

不需要账号、不需要联网（雷达部分用伪造的接口数据），主要覆盖「私人雷达」
这种没法在匿名状态下真机验证的逻辑。

    python tools/test_netease_offline.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from netease_music import api, crypto, utils  # noqa: E402

FAILED = []


def check(name, condition, detail=""):
    print("  [%s] %s%s" % ("通过" if condition else "失败", name, "" if condition else "  " + str(detail)))
    if not condition:
        FAILED.append(name)


#: 造一份和首页 block page 结构相同的假数据（私人雷达是「影子歌单」）
FAKE_HOMEPAGE = {
    "code": 200,
    "data": {
        "blocks": [
            {
                "blockCode": "HOMEPAGE_BLOCK_PLAYLIST_RCMD",
                "creatives": [
                    {
                        "uiElement": {"title": "每日推荐"},
                        "resources": [{"resourceId": "111111", "resourceType": "playlist"}],
                    },
                    {
                        "creativeType": "shadow_playlist",
                        "uiElement": {"title": "私人雷达"},
                        "resources": [{"resourceId": "31313131", "resourceType": "playlist"}],
                    },
                ],
            }
        ]
    },
}

FAKE_USER_PLAYLISTS = {
    "code": 200,
    "playlist": [
        {"id": 501, "name": "我喜欢的音乐", "trackCount": 1024, "specialType": 5},
        {"id": 502, "name": "私人雷达", "trackCount": 30, "specialType": 0},
        {"id": 503, "name": "摇滚收藏", "trackCount": 88, "specialType": 0},
    ],
}


def test_normalize():
    print("== 数据标准化 ==")
    track = api.normalize_track({
        "id": 33894312, "name": "海阔天空",
        "ar": [{"name": "Beyond"}], "al": {"name": "乐与怒"},
        "dt": 267230, "fee": 1, "mv": 0,
    })
    check("歌曲 ID 转字符串", track["id"] == "33894312", track)
    check("歌手拼接", track["artists"] == "Beyond", track)
    check("时长", track["duration"] == 267230, track)
    check("时长显示", utils.format_duration(track["duration"]) == "04:27")

    old_style = api.normalize_track({"id": 1, "name": "x", "artists": [{"name": "A"}, {"name": "B"}],
                                     "album": {"name": "专辑"}, "duration": 1000})
    check("兼容 artists/album 老字段", old_style["artists"] == "A / B" and old_style["album"] == "专辑", old_style)

    playlist = api.normalize_playlist({"id": 501, "name": "我喜欢的音乐", "trackCount": 1024,
                                       "specialType": 5, "creator": {"nickname": "我"}})
    check("歌单 specialType", playlist["special_type"] == 5, playlist)
    check("歌单曲目数", playlist["track_count"] == 1024, playlist)


def test_radar_keywords():
    print("== 私人雷达定位 ==")
    pid, title = api.find_playlist_by_keywords(FAKE_HOMEPAGE, ("私人雷达", "雷达"))
    check("从首页推荐流里找到雷达歌单", pid == "31313131", (pid, title))
    check("标题正确", title == "私人雷达", title)

    pid, title = api.find_playlist_by_keywords(FAKE_USER_PLAYLISTS, ("私人雷达", "雷达"))
    check("从我的歌单里找到雷达歌单", pid == "502", (pid, title))

    pid, title = api.find_playlist_by_keywords({"data": {"blocks": []}}, ("私人雷达",))
    check("找不到时返回空", pid is None and title is None, (pid, title))


class FakeClient(api.NeteaseClient):
    """伪造三种数据源，验证 find_radar_playlist 的降级顺序。"""

    def __init__(self, sources):
        super().__init__()
        self.sources = sources
        self.calls = []
        self.profile = {"userId": 42}

    def user_playlists(self, uid, limit=1000, offset=0):
        self.calls.append("user_playlists")
        return self.sources["user_playlists"]()

    def call(self, path, payload=None, transports=None, timeout=None):
        self.calls.append(path)
        if "homepage" in path:
            return self.sources["homepage"]()
        if "recommend/resource" in path:
            return self.sources["resource"]()
        raise AssertionError("未预期的路径 %s" % path)

    def recommend_resource(self):
        self.calls.append("recommend_resource")
        return self.sources["resource"]()


def _raise(exc):
    def inner():
        raise exc
    return inner


def test_radar_resolution():
    print("== 私人雷达降级顺序 ==")
    client = FakeClient({
        "user_playlists": lambda: [api.normalize_playlist(pl) for pl in FAKE_USER_PLAYLISTS["playlist"]],
        "homepage": _raise(api.ApiError("需要登录", 301, True)),
        "resource": _raise(api.ApiError("需要登录", 301, True)),
    })
    radar = client.find_radar_playlist()
    check("优先用我的歌单", radar["id"] == "502" and radar["source"] == "我的歌单", radar)

    client = FakeClient({
        "user_playlists": lambda: [],
        "homepage": lambda: FAKE_HOMEPAGE,
        "resource": _raise(api.ApiError("需要登录", 301, True)),
    })
    radar = client.find_radar_playlist()
    check("歌单里没有就用首页推荐流", radar["id"] == "31313131" and radar["source"] == "首页推荐流", radar)

    client = FakeClient({
        "user_playlists": _raise(api.ApiError("需要登录", 301, True)),
        "homepage": _raise(api.ApiError("接口未找到", 404)),
        "resource": lambda: [api.normalize_playlist({"id": 777, "name": "私人雷达", "trackCount": 30})],
    })
    radar = client.find_radar_playlist()
    check("前两个都失败就退到每日推荐歌单", radar["id"] == "777", radar)

    client = FakeClient({
        "user_playlists": _raise(api.ApiError("需要登录", 301, True)),
        "homepage": _raise(api.ApiError("接口未找到", 404)),
        "resource": _raise(api.ApiError("需要登录", 301, True)),
    })
    try:
        client.find_radar_playlist()
        check("全都失败时抛出可读错误", False, "没有抛异常")
    except api.ApiError as exc:
        check("全都失败时抛出可读错误", "私人雷达" in str(exc) and "歌单 ID" in str(exc), str(exc)[:80])

    client = FakeClient({"user_playlists": lambda: [], "homepage": lambda: {}, "resource": lambda: []})
    radar = client.find_radar_playlist(manual_id="88888")
    check("手动指定优先", radar["id"] == "88888" and "手动" in radar["name"], radar)


def test_cookie():
    print("== Cookie 处理 ==")
    jar = api.NeteaseClient.parse_cookie_string("MUSIC_U=abc123; __csrf=deadbeef; os=pc")
    check("解析 Cookie", jar["MUSIC_U"] == "abc123" and jar["__csrf"] == "deadbeef", jar)
    client = api.NeteaseClient(cookie="MUSIC_U=old; __csrf=x")
    client.merge_set_cookie({"set-cookie": "MUSIC_U=new; Path=/; HttpOnly, NMTID=999; Path=/"})
    merged = api.NeteaseClient.parse_cookie_string(client.cookie)
    check("合并 Set-Cookie", merged["MUSIC_U"] == "new" and merged["NMTID"] == "999" and merged["__csrf"] == "x", merged)
    client.real_ip = "116.25.146.177"
    check("附加 RealIP", "RealIP=116.25.146.177" in client.cookie_with_extras("os=pc"), client.cookie_with_extras("os=pc"))
    check("csrf 读取", client.csrf == "x", client.csrf)


def test_transport_build():
    print("== 三种协议构造 ==")
    client = api.NeteaseClient(cookie="MUSIC_U=abc")
    url, form, headers = client._build("eapi", "/api/song/url", {"id": 1})
    check("eapi 地址", url == "https://interface.music.163.com/eapi/song/url", url)
    check("eapi 参数为 params", list(form.keys()) == ["params"], list(form.keys()))
    check("eapi 带 App UA", "NeteaseMusic" in headers["User-Agent"], headers["User-Agent"])

    url, form, headers = client._build("plain", "/api/song/url", {"id": 1})
    check("普通接口明文参数", url == "https://music.163.com/api/song/url" and form == {"id": 1}, (url, form))

    url, form, headers = client._build("weapi", "/api/song/url", {"id": 1})
    check("weapi 地址", url == "https://music.163.com/weapi/song/url", url)
    check("weapi 参数", sorted(form.keys()) == ["encSecKey", "params"], sorted(form.keys()))

    check("路径归一化", api.normalize_path("v6/playlist/detail") == "/api/v6/playlist/detail")
    check("路径归一化（已带 /api）", api.normalize_path("/api/x") == "/api/x")


def test_quality_fallback():
    print("== 音质回退 ==")
    check("有则用首选", utils.pick_quality("lossless", {"lossless": True}) == "lossless")
    check("首选不可用则降级", utils.pick_quality("lossless", {"exhigh": True}) == "exhigh")
    check("全都没有则保持", utils.pick_quality("exhigh", {}) == "exhigh")


class RecordingClient(api.NeteaseClient):
    """只记录请求体，不真的发网络请求。"""

    def __init__(self):
        super().__init__()
        self.last = None

    def call(self, path, payload=None, transports=None, timeout=None):
        self.last = (path, dict(payload or {}))
        return {"code": 200, "ids": ["1", "2"]}


class FakeLikeClient(RecordingClient):
    def __init__(self, like_result=None, manipulate_result=None):
        super().__init__()
        self.like_result = like_result
        self.manipulate_result = manipulate_result
        self.calls = []

    def song_like(self, song_id, like=True):
        self.calls.append(("song_like", song_id, like))
        if isinstance(self.like_result, Exception):
            raise self.like_result
        return self.like_result if self.like_result is not None else {"code": 200}

    def playlist_manipulate_tracks(self, playlist_id, song_ids, op="add"):
        self.calls.append(("manipulate", playlist_id, list(song_ids), op))
        if isinstance(self.manipulate_result, Exception):
            raise self.manipulate_result
        return self.manipulate_result if self.manipulate_result is not None else {"code": 200}


def test_like():
    print("== 加入我喜欢的音乐 ==")
    client = RecordingClient()
    client.song_like("347230", True)
    check("like 参数是字符串 true", client.last[1]["like"] == "true", client.last)
    client.song_like("347230", False)
    check("取消喜欢是字符串 false", client.last[1]["like"] == "false", client.last)
    check("路径正确", client.last[0] == "/api/song/like", client.last[0])
    check("带 trackId", client.last[1]["trackId"] == "347230", client.last[1])
    check("likelist 解析成字符串集合", client.likelist("99") == {"1", "2"})

    client = FakeLikeClient()
    result = client.set_like("347230", True, "501")
    check("优先走喜欢接口", result["via"] == "喜欢接口" and result["ok"], result)
    check("只调用了一次", client.calls == [("song_like", "347230", True)], client.calls)

    client = FakeLikeClient(like_result=api.ApiError("需要登录", 301, True))
    try:
        client.set_like("347230", True, "501")
        check("需要登录时直接抛出、不误用备用接口", False, "没有抛异常")
    except api.ApiError as exc:
        check("需要登录时直接抛出、不误用备用接口", exc.need_login and client.calls == [("song_like", "347230", True)],
              (exc, client.calls))

    client = FakeLikeClient(like_result=api.ApiError("接口未找到", 404))
    result = client.set_like("347230", True, "501")
    check("喜欢接口不可用时退回歌单接口",
          result["via"] == "歌单接口（备用）" and client.calls[-1] == ("manipulate", "501", ["347230"], "add"),
          (result, client.calls))

    client = FakeLikeClient(like_result=api.ApiError("接口未找到", 404))
    try:
        client.set_like("347230", True, "")
        check("没有歌单 ID 时不硬退回", False, "没有抛异常")
    except api.ApiError:
        check("没有歌单 ID 时不硬退回", len(client.calls) == 1, client.calls)

    client = FakeLikeClient(like_result=api.ApiError("接口未找到", 404))
    try:
        client.set_like("347230", False, "501")
        check("取消喜欢不走“加歌”接口", False, "没有抛异常")
    except api.ApiError:
        check("取消喜欢不走“加歌”接口", len(client.calls) == 1, client.calls)


def main():
    test_normalize()
    test_radar_keywords()
    test_radar_resolution()
    test_cookie()
    test_transport_build()
    test_quality_fallback()
    test_like()
    print()
    if FAILED:
        print("失败项：%s" % "、".join(FAILED))
        return 1
    print("全部离线用例通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
