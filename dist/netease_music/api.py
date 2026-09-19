# -*- coding: utf-8 -*-
"""网易云音乐接口客户端。

网易云同时存在三套接口，**同一台机器上不一定都能用**，所以这里做了自动降级：

===========  ==========================================  ==========================
协议          地址                                        说明
===========  ==========================================  ==========================
``eapi``     ``interface.music.163.com/eapi/api/xxx``     移动端接口，AES-ECB 加密
``plain``    ``music.163.com/api/xxx``                    普通接口，只用 Cookie
``weapi``    ``music.163.com/weapi/api/xxx``              网页接口，双层 AES + RSA
===========  ==========================================  ==========================

三者的参数名不同（eapi/weapi 加密成 ``params``，plain 直接明文），但业务参数与
返回结构基本一致。客户端会按“历史成功率”排序依次尝试，某条链路返回空响应或报
“接口未找到”时自动换下一条，并把结果记在面板的“自检”里，方便排查网络问题。
"""

from __future__ import annotations

import json
import time

from . import crypto, utils

WEB_HOST = "https://music.163.com"
INTERFACE_HOST = "https://interface.music.163.com"

BROWSER_UA = utils.USER_AGENT
APP_UA = "NeteaseMusic/9.1.65.240927161425(9001065);Dalvik/2.1.0 (Linux; U; Android 14; zh_CN)"
APP_COOKIE = "os=android; appver=9.1.65; channel=netease; deviceId=blender_netease_music"

TRANSPORTS = ("eapi", "plain", "weapi")
TRANSPORT_LABELS = {
    "eapi": "eapi（App 接口）",
    "plain": "普通 /api",
    "weapi": "weapi（网页接口）",
}

#: 需要登录才能用的业务码
NEED_LOGIN_CODES = {301, 302, 250}
#: 表示“这条链路/这个路径不通”，值得换一种协议再试
ROUTE_PROBLEM_CODES = {404, -460, 50002, 400}


class ApiError(RuntimeError):
    """接口层错误。``code`` 为网易云业务码，``need_login`` 标记未登录。"""

    def __init__(self, message: str, code=None, need_login: bool = False, transport: str = ""):
        super().__init__(message)
        self.code = code
        self.need_login = need_login
        self.transport = transport


def normalize_path(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    if not path.startswith("/api/"):
        path = "/api" + path
    return path


# --------------------------------------------------------------------------
# 数据标准化
# --------------------------------------------------------------------------


def normalize_track(song: dict) -> dict:
    """把接口返回的歌曲对象压成插件内部统一结构。"""
    if not isinstance(song, dict):
        return {}
    artists = song.get("ar") or song.get("artists") or []
    if isinstance(artists, dict):
        artists = [artists]
    names = [a.get("name") for a in artists if isinstance(a, dict) and a.get("name")]
    album = song.get("al") or song.get("album") or {}
    if not isinstance(album, dict):
        album = {}
    # 「我喜欢的音乐」等接口会用 privilege/st 描述可播放性
    privilege = song.get("privilege") or {}
    fee = song.get("fee", privilege.get("fee", 0))
    return {
        "id": str(song.get("id") or ""),
        "name": song.get("name") or "未知歌曲",
        "artists": " / ".join(names) if names else "未知歌手",
        "album": album.get("name") or "",
        "duration": int(song.get("dt") or song.get("duration") or 0),
        "fee": int(fee or 0),
        "mv_id": str(song.get("mv") or song.get("mvid") or 0),
        "no_copyright": bool(song.get("noCopyrightRcmd")),
    }


def normalize_playlist(playlist: dict) -> dict:
    creator = playlist.get("creator") or {}
    if not isinstance(creator, dict):
        creator = {}
    return {
        "id": str(playlist.get("id") or ""),
        "name": playlist.get("name") or "未命名歌单",
        "track_count": int(playlist.get("trackCount") or 0),
        "creator": creator.get("nickname") or "",
        "special_type": int(playlist.get("specialType") or 0),
        "subscribed": bool(playlist.get("subscribed")),
        "description": (playlist.get("description") or "").strip(),
    }


def normalize_song_url(item: dict) -> dict:
    if not isinstance(item, dict):
        return {}
    return {
        "url": item.get("url") or "",
        "br": int(item.get("br") or 0),
        "size": int(item.get("size") or 0),
        "type": (item.get("type") or "").lower(),
        "level": item.get("level") or "",
        "fee": item.get("fee"),
        "code": item.get("code"),
        "md5": item.get("md5") or "",
    }


def _walk_json(node, out):
    """递归收集 (标题, 资源 id) 候选，用于在首页数据里找“私人雷达”。"""
    if isinstance(node, dict):
        title = node.get("name") or node.get("title")
        ui = node.get("uiElement")
        if isinstance(ui, dict) and ui.get("title"):
            title = ui["title"]
        pid = node.get("resourceId") or node.get("id")
        if title and pid not in (None, ""):
            out.append((str(title), str(pid)))
        resources = node.get("resources")
        if isinstance(resources, list):
            for res in resources:
                if isinstance(res, dict):
                    rid = res.get("resourceId") or res.get("id")
                    rname = (res.get("uiElement") or {}).get("title") if isinstance(res.get("uiElement"), dict) else None
                    if rid not in (None, "") and (rname or title):
                        out.append((str(rname or title), str(rid)))
        for value in node.values():
            _walk_json(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_json(item, out)


def find_playlist_by_keywords(payload, keywords) -> tuple:
    """在任意接口返回里按标题关键词找歌单，返回 ``(id, 名称)`` 或 ``(None, None)``。"""
    candidates = []
    _walk_json(payload, candidates)
    for keyword in keywords:
        for title, pid in candidates:
            if keyword in title and pid.isdigit():
                return pid, title
    return None, None


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class NeteaseClient:
    """一个会话（Cookie + 协议偏好 + 统计）。"""

    def __init__(
        self,
        cookie: str = "",
        real_ip: str = "",
        transports=None,
        preferred: str = "auto",
        timeout: float = 20.0,
        debug: bool = False,
    ):
        self.cookie = (cookie or "").strip()
        self.real_ip = (real_ip or "").strip()
        self.timeout = float(timeout)
        self.debug = bool(debug)
        self.preferred = preferred or "auto"
        self._penalty = {name: 0 for name in TRANSPORTS}
        self.last_transport = ""
        self.transport_log = []          # [(协议, 路径, 结果)] 供面板“自检”显示
        self.profile = {}
        self.account = {}

    # ---------------------------------------------------------------- Cookie

    def set_cookie(self, cookie: str):
        self.cookie = (cookie or "").strip()

    def cookie_with_extras(self, extra: str = "") -> str:
        parts = [self.cookie] if self.cookie else []
        if extra:
            parts.append(extra)
        if self.real_ip:
            parts.append("RealIP=%s" % self.real_ip)
        return "; ".join(part for part in parts if part)

    @staticmethod
    def parse_cookie_string(text: str) -> dict:
        result = {}
        for chunk in (text or "").replace("\n", ";").split(";"):
            if "=" in chunk:
                key, _, value = chunk.partition("=")
                key = key.strip()
                if key:
                    result[key] = value.strip()
        return result

    def merge_set_cookie(self, headers: dict):
        """把响应里的 ``Set-Cookie`` 合并进会话 Cookie。"""
        raw = headers.get("set-cookie") or ""
        if not raw:
            return
        jar = self.parse_cookie_string(self.cookie)
        for part in raw.split(","):
            key, sep, value = part.partition("=")
            if sep and key.strip():
                jar[key.strip()] = value.split(";")[0].strip()
        self.cookie = "; ".join("%s=%s" % (k, v) for k, v in jar.items() if v)

    @property
    def csrf(self) -> str:
        return self.parse_cookie_string(self.cookie).get("__csrf", "")

    # ---------------------------------------------------------------- 请求

    def _order(self):
        if self.preferred in TRANSPORTS:
            rest = [name for name in TRANSPORTS if name != self.preferred]
            return [self.preferred] + sorted(rest, key=lambda n: self._penalty[n])
        return sorted(TRANSPORTS, key=lambda name: (self._penalty[name], TRANSPORTS.index(name)))

    def _build(self, transport: str, path: str, payload: dict):
        if transport == "eapi":
            form = crypto.eapi_params(path, payload)
            url = INTERFACE_HOST + "/eapi" + path[len("/api"):]
            headers = {
                "User-Agent": APP_UA,
                "Referer": "https://music.163.com/",
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": self.cookie_with_extras(APP_COOKIE),
            }
        elif transport == "plain":
            form = payload
            url = WEB_HOST + path
            headers = {"User-Agent": BROWSER_UA, "Referer": "https://music.163.com/"}
        else:
            form = crypto.weapi_params(payload)
            url = WEB_HOST + "/weapi" + path[len("/api"):]
            headers = {"User-Agent": BROWSER_UA, "Referer": "https://music.163.com/"}
        return url, form, headers

    def call(self, path: str, payload: dict | None = None, transports=None, timeout: float | None = None) -> dict:
        """按协议优先级请求，返回接口原始 JSON。

        三种链路都失败时抛出最后一个错误；服务端明确回答“需要登录”时立即抛出。
        """
        path = normalize_path(path)
        payload = dict(payload or {})
        all_failed = []
        for transport in (transports or self._order()):
            url, form, headers = self._build(transport, path, payload)
            try:
                data, resp_headers = utils.http_post_form_ex(
                    url, form, extra_headers=headers,
                    timeout=timeout or self.timeout, retries=0,
                )
            except utils.EmptyResponseError as exc:
                self._penalty[transport] += 2
                all_failed.append((transport, "%s" % exc))
                self.transport_log.append((transport, path, "空响应"))
                utils.log_debug("%s 返回空响应：%s" % (transport, path))
                continue
            except utils.HttpError as exc:
                self._penalty[transport] += 2
                all_failed.append((transport, "%s" % exc))
                self.transport_log.append((transport, path, str(exc)))
                continue

            self.merge_set_cookie(resp_headers)
            code = data.get("code")
            if code == 200:
                self._penalty[transport] = max(0, self._penalty[transport] - 1)
                self.last_transport = transport
                self.transport_log.append((transport, path, "200"))
                if len(self.transport_log) > 200:
                    del self.transport_log[:100]
                return data

            message = data.get("message") or data.get("msg") or ("业务码 %s" % code)
            self.transport_log.append((transport, path, "code=%s" % code))
            if code in NEED_LOGIN_CODES:
                raise ApiError("需要登录后才能使用该功能（%s）" % message, code, True, transport)
            if code in ROUTE_PROBLEM_CODES:
                all_failed.append((transport, "%s" % message))
                self._penalty[transport] += 1
                continue
            raise ApiError("%s（业务码 %s）" % (message, code), code, False, transport)

        detail = "；".join("%s: %s" % (TRANSPORT_LABELS.get(name, name), err) for name, err in all_failed)
        raise ApiError(
            "三种接口都无法访问（可能是网络环境限制）。%s\n"
            "建议：优先使用「Cookie 导入」登录，或在偏好设置里手动指定首选协议。" % (detail or "无可用链路")
        )

    # ---------------------------------------------------------------- 账号

    def login_status(self) -> dict:
        data = self.call("/api/w/nuser/account/get", {})
        self.account = data.get("account") or {}
        self.profile = data.get("profile") or {}
        return {
            "logged_in": bool(self.account),
            "user_id": str((self.profile or {}).get("userId") or (self.account or {}).get("id") or ""),
            "nickname": (self.profile or {}).get("nickname") or "",
            "vip_type": int((self.account or {}).get("vipType") or 0),
        }

    def logout(self) -> None:
        try:
            self.call("/api/logout", {})
        except Exception:  # noqa: BLE001 - 退出失败也要清本地 Cookie
            pass

    # ---------------------------------------------------------------- 歌单

    def user_playlists(self, uid: str, limit: int = 1000, offset: int = 0) -> list:
        data = self.call("/api/user/playlist", {"uid": str(uid), "limit": limit, "offset": offset})
        items = data.get("playlist") or []
        return [normalize_playlist(pl) for pl in items if isinstance(pl, dict)]

    def playlist_detail(self, playlist_id: str, track_limit: int = 1000) -> dict:
        data = self.call("/api/v6/playlist/detail", {"id": playlist_id, "n": track_limit, "s": 8})
        playlist = data.get("playlist") or {}
        tracks = [normalize_track(t) for t in (playlist.get("tracks") or [])]
        track_ids = []
        for item in playlist.get("trackIds") or []:
            if isinstance(item, dict) and item.get("id"):
                track_ids.append(str(item["id"]))
            elif isinstance(item, (int, str)):
                track_ids.append(str(item))
        info = normalize_playlist(playlist)
        info["tracks"] = tracks
        info["track_ids"] = track_ids
        return info

    def songs_detail(self, song_ids, batch: int = 200) -> list:
        """批量取歌曲详情（大歌单用它补齐 ``trackIds`` 里没带完整信息的歌）。"""
        songs = []
        ids = [str(i) for i in song_ids if str(i).isdigit()]
        for start in range(0, len(ids), batch):
            chunk = ids[start:start + batch]
            data = self.call("/api/song/detail", {"ids": json.dumps(chunk, separators=(",", ":"))})
            for song in data.get("songs") or []:
                songs.append(normalize_track(song))
        return songs

    # ---------------------------------------------------------------- 推荐

    def daily_recommend(self) -> list:
        data = self.call("/api/v3/discovery/recommend/songs", {"offset": 0, "limit": 100, "total": True})
        inner = data.get("data") or {}
        return [normalize_track(song) for song in (inner.get("dailySongs") or [])]

    def recommend_resource(self) -> list:
        data = self.call("/api/v1/discovery/recommend/resource", {})
        inner = data.get("data") or data.get("recommend") or []
        if isinstance(inner, dict):
            inner = inner.get("recommend") or []
        return [normalize_playlist(pl) for pl in inner if isinstance(pl, dict)]

    def find_radar_playlist(self, manual_id: str = "") -> dict:
        """定位“私人雷达”歌单。

        官方没有单独的“私人雷达”接口，它属于首页 / 推荐流里的“影子歌单”，
        所以这里按可靠性从高到低依次尝试，最后还能手动指定。
        """
        if manual_id:
            manual_id = str(manual_id).strip()
            if manual_id:
                return {"id": manual_id, "name": "私人雷达（手动指定）", "source": "偏好设置"}

        attempts = []
        if self.profile.get("userId"):
            attempts.append(("我的歌单", lambda: self.user_playlists(str(self.profile["userId"]))))
        attempts.append(("首页推荐流", lambda: self.call("/api/homepage/block/page", {"refresh": True, "cursor": 0})))
        attempts.append(("每日推荐歌单", lambda: self.recommend_resource()))

        last_error = None
        for source, loader in attempts:
            try:
                payload = loader()
            except ApiError as exc:
                last_error = exc
                continue
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
            pid, title = find_playlist_by_keywords(payload, ("私人雷达", "雷达"))
            if pid:
                return {"id": pid, "name": title, "source": source}
        hint = ""
        if last_error:
            hint = "（最后一条错误：%s）" % last_error
        raise ApiError(
            "没能自动找到「私人雷达」。它需要登录后才会出现在首页推荐流里。%s\n"
            "可以在偏好设置里手动填写私人雷达歌单 ID（在歌单页面链接的 id= 后面）。" % hint
        )

    # ---------------------------------------------------------------- 播放

    def song_url(self, song_id: str, level: str = "exhigh") -> dict:
        """取播放直链；请求等级拿不到时按音质阶梯逐级降级重试。"""
        ladder = [key for key, _ in utils.QUALITY_LABELS]
        level = utils.pick_quality(level, None)
        index = ladder.index(level) if level in ladder else 2
        order = [level] + [key for key in reversed(ladder[:index])]
        last = {}
        for item in order:
            data = self.call("/api/song/enhance/player/url/v1", {
                "ids": json.dumps([str(song_id)], separators=(",", ":")),
                "level": item,
                "encodeType": "flac" if item in ("lossless", "hires") else "aac",
            })
            rows = data.get("data") or []
            info = normalize_song_url(rows[0]) if rows else {}
            last = info
            if info.get("url"):
                info["requested_level"] = item
                return info
        if last:
            reason = last.get("code")
            raise ApiError("这首歌没有可用的播放地址（可能无版权、需要会员或已下架，接口码 %s）" % reason)
        raise ApiError("接口没有返回播放地址")

    def lyric(self, song_id: str) -> dict:
        """取歌词。

        返回 ``{"lrc": 原文 LRC, "translated": 翻译 LRC, "merged": 面板用的整段文本}``：
        动态歌词浮层需要原始 LRC 来解析时间轴，面板则直接用整段文本。
        """
        data = self.call("/api/song/lyric", {"id": str(song_id), "lv": -1, "kv": -1, "tv": -1})
        original = ((data.get("lrc") or {}).get("lyric") or "").strip()
        translated = ((data.get("tlyric") or {}).get("lyric") or "").strip()
        merged = original
        if original and translated:
            merged = original + "\n\n—— 翻译 ——\n" + translated
        return {
            "lrc": original,
            "translated": translated,
            "merged": merged or "（这首歌没有歌词）",
        }

    def likelist(self, uid: str) -> set:
        """账号里「我喜欢的音乐」的全部歌曲 ID。"""
        data = self.call("/api/song/like/get", {"uid": str(uid)})
        return {str(i) for i in (data.get("ids") or [])}

    def song_like(self, song_id: str, like: bool = True) -> dict:
        """喜欢 / 取消喜欢（注意 ``like`` 必须是字符串，布尔值会被判参数错误）。"""
        return self.call("/api/song/like", {
            "trackId": str(song_id),
            "like": "true" if like else "false",
            "timestamp": int(time.time() * 1000),
        })

    def playlist_manipulate_tracks(self, playlist_id: str, song_ids, op: str = "add") -> dict:
        """把歌曲加入 / 移出指定歌单（收藏接口不可用时的备用通道）。"""
        return self.call("/api/playlist/manipulate/tracks", {
            "pid": str(playlist_id),
            "trackIds": json.dumps([str(i) for i in song_ids], separators=(",", ":")),
            "op": op,
            "imme": "true",
        })

    def set_like(self, song_id: str, like: bool = True, liked_playlist_id: str = "") -> dict:
        """加入 / 移出「我喜欢的音乐」。

        首选官方的 ``/api/song/like``；如果这条路径在当前链路上不存在
        （返回 404 / 50002 之类的“接口未找到”），就退回用「往我喜欢的音乐歌单里
        加歌」的接口。两条都属于需要登录的操作。

        返回 ``{"ok": bool, "via": 说明, "code": 业务码}``。
        """
        try:
            data = self.song_like(song_id, like)
            return {"ok": True, "via": "喜欢接口", "code": data.get("code")}
        except ApiError as exc:
            if exc.need_login:
                raise
            if like and liked_playlist_id:
                data = self.playlist_manipulate_tracks(liked_playlist_id, [song_id], "add")
                return {"ok": True, "via": "歌单接口（备用）", "code": data.get("code")}
            raise
