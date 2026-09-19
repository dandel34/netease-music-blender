# -*- coding: utf-8 -*-
"""通用工具：HTTP 请求、时间格式化、文件名清理、缓存目录。"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__all__ = [
    "HttpError",
    "http_post_form",
    "http_get_json",
    "format_duration",
    "millis_to_hms",
    "pick_quality",
    "QUALITY_LABELS",
    "clean_filename",
    "ensure_dir",
    "log",
    "debug_mode",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://music.163.com/",
    "Origin": "https://music.163.com",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "close",
}

QUALITY_LABELS = (
    ("standard", "标准 128k"),
    ("higher", "较高 192k"),
    ("exhigh", "极高 320k"),
    ("lossless", "无损 FLAC"),
    ("hires", "Hi-Res"),
)

_DEBUG = {"on": False}


def debug_mode(enabled: bool) -> None:
    _DEBUG["on"] = bool(enabled)


def _print_safe(prefix: str, text: str) -> None:
    """打印日志；某些终端不支持中文/特殊符号，绝不能因为日志把功能搞崩。"""
    try:
        print(prefix, text)
    except UnicodeEncodeError:
        try:
            print("[NetEase Music]", text.encode("ascii", "replace").decode("ascii"))
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def log(*args) -> None:
    """统一日志前缀，方便在 Blender 控制台里过滤。"""
    _print_safe("[网易云音乐]", " ".join(str(arg) for arg in args))


def log_debug(*args) -> None:
    if _DEBUG["on"]:
        _print_safe("[网易云音乐·调试]", " ".join(str(arg) for arg in args))


class HttpError(RuntimeError):
    """网络层错误（统一成中文描述，便于直接显示在面板上）。"""

    def __init__(self, message: str, status: int | None = None, url: str = ""):
        super().__init__(message)
        self.status = status
        self.url = url


class EmptyResponseError(HttpError):
    """HTTP 200 但响应体为空——网易云在某些链路上会这样“静默拒绝”请求。

    客户端据此换用另一种协议（eapi / 普通 /api / weapi）重试。
    """


def _ssl_context():
    """尽量使用系统证书；若环境缺少证书则退回非校验模式（仅影响 HTTPS 抓取）。"""
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        return ssl.create_default_context()
    except Exception:
        return ssl._create_unverified_context()


_CTX = None


def _context():
    global _CTX
    if _CTX is None:
        _CTX = _ssl_context()
    return _CTX


_UNVERIFIED = None


def _unverified_context():
    global _UNVERIFIED
    if _UNVERIFIED is None:
        _UNVERIFIED = ssl._create_unverified_context()
    return _UNVERIFIED


def http_post_form(
    url: str,
    form: dict,
    cookie: str = "",
    extra_headers: dict | None = None,
    timeout: float = 15.0,
    retries: int = 2,
) -> dict:
    """POST ``application/x-www-form-urlencoded``，返回解析后的 JSON。"""
    data, _ = http_post_form_ex(url, form, cookie, extra_headers, timeout, retries)
    return data


def http_post_form_ex(
    url: str,
    form: dict,
    cookie: str = "",
    extra_headers: dict | None = None,
    timeout: float = 15.0,
    retries: int = 2,
) -> tuple:
    """同上，但额外返回响应头（登录时要从 ``Set-Cookie`` 里取 ``MUSIC_U``）。"""
    body = urllib.parse.urlencode(form).encode("utf-8")
    headers = dict(DEFAULT_HEADERS)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = cookie
    if extra_headers:
        headers.update(extra_headers)
    return _request_json(url, body, headers, timeout, retries, want_headers=True)


def http_get_json(
    url: str,
    params: dict | None = None,
    cookie: str = "",
    extra_headers: dict | None = None,
    timeout: float = 15.0,
    retries: int = 2,
) -> dict:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = dict(DEFAULT_HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    if extra_headers:
        headers.update(extra_headers)
    return _request_json(url, None, headers, timeout, retries)


def _request_json(url: str, body: bytes | None, headers: dict, timeout: float, retries: int,
                  want_headers: bool = False):
    last_error = None
    force_unverified = False
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
        try:
            ctx = _unverified_context() if force_unverified else _context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read()
                status = resp.status
                resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                raise EmptyResponseError("服务器返回了空内容（HTTP %s）" % status, status, url)
            try:
                payload = json.loads(text)
            except ValueError:
                snippet = re.sub(r"\s+", " ", text)[:200]
                raise HttpError("返回内容不是 JSON：%s" % snippet, status, url)
            return (payload, resp_headers) if want_headers else payload
        except urllib.error.HTTPError as exc:
            last_error = HttpError("HTTP %s %s" % (exc.code, exc.reason), exc.code, url)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLError) and not force_unverified:
                # Blender 自带的 Python 有时取不到系统证书，退一步不校验（仅影响读取数据）
                log("HTTPS 证书校验失败，改用不校验证书的连接重试：%s" % reason)
                force_unverified = True
                continue
            last_error = HttpError("网络连接失败：%s" % reason, None, url)
        except EmptyResponseError as exc:
            last_error = exc
        except HttpError as exc:
            last_error = exc
        except TimeoutError:
            last_error = HttpError("请求超时（%.0f 秒）" % timeout, None, url)
        except Exception as exc:  # noqa: BLE001 - 统一兜底成可读错误
            last_error = HttpError("请求异常：%s: %s" % (type(exc).__name__, exc), None, url)
        if attempt < retries:
            time.sleep(0.6 * (attempt + 1))
    raise last_error if last_error else HttpError("未知网络错误", None, url)


def format_duration(milliseconds) -> str:
    """毫秒 → ``mm:ss``。"""
    try:
        total = int(round(float(milliseconds) / 1000.0))
    except (TypeError, ValueError):
        return "--:--"
    if total < 0:
        total = 0
    return "%02d:%02d" % (total // 60, total % 60)


def millis_to_hms(milliseconds) -> str:
    try:
        total = int(milliseconds) // 1000
    except (TypeError, ValueError):
        return "0:00:00"
    return "%d:%02d:%02d" % (total // 3600, (total % 3600) // 60, total % 60)


def pick_quality(preferred: str, available: dict | None = None) -> str:
    """在偏好等级不可用时按标准顺序回退。"""
    order = [key for key, _ in QUALITY_LABELS]
    if preferred not in order:
        preferred = "exhigh"
    if not available:
        return preferred
    index = order.index(preferred)
    for key in order[index:]:
        if available.get(key):
            return key
    for key in reversed(order[:index]):
        if available.get(key):
            return key
    return preferred


_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def clean_filename(name: str, max_length: int = 120) -> str:
    """把歌名清洗成合法文件名（Windows 全套非法字符）。"""
    cleaned = _INVALID_CHARS.sub("_", str(name or "unknown")).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "unknown"
    return cleaned[:max_length]


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def default_cache_dir() -> str:
    """默认放音视频缓存的位置（跟随系统临时目录，避免污染工程目录）。"""
    base = os.environ.get("TEMP") or os.environ.get("TMPDIR") or os.path.expanduser("~")
    return os.path.join(base, "blender_netease_music")


def is_blender() -> bool:
    return "bpy" in sys.modules
