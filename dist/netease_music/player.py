# -*- coding: utf-8 -*-
"""播放引擎：用 Blender 自带的 ``aud``（audaspace）模块在 Blender 内部播放音频。

Blender 的 Python 里编译进了 ``aud`` 模块（文件浏览器的试听、序列预览器都用它），
所以插件不需要任何外部播放器，也不需要额外依赖：

    device = aud.Device()
    handle = device.play(aud.Sound.file(path))

关于 ``aud`` 的两处实测结论（Blender 5.2）：

* ``Sound.length`` 是**采样帧数**而不是秒，需要除以 ``Sound.specs[0]``（采样率）；
* ``Handle.status`` 返回的是布尔值（True = 还在放，False = 已结束），
  并不像老版本文档那样返回 ``STATUS_*`` 枚举，所以暂停状态由本模块自己记录。

``aud`` 不可用时（极少见的自编译版本）会给出明确提示，并支持退回系统播放器。
下载、缓存也在这里统一处理。
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request

from . import utils

try:  # pragma: no cover - 取决于 Blender 构建
    import aud as _aud
except Exception:  # noqa: BLE001
    _aud = None

#: 音质 → 容器扩展名（用于缓存文件命名）
QUALITY_EXT = {
    "standard": "mp3",
    "higher": "mp3",
    "exhigh": "m4a",
    "lossless": "flac",
    "hires": "flac",
}

_STATUS_PLAYING = getattr(_aud, "STATUS_PLAYING", 1) if _aud else 1
_STATUS_PAUSED = getattr(_aud, "STATUS_PAUSED", 2) if _aud else 2
_STATUS_STOPPED = getattr(_aud, "STATUS_STOPPED", 3) if _aud else 3


def aud_available() -> bool:
    return _aud is not None


def aud_error() -> str:
    if _aud is None:
        return "当前 Blender 构建没有编译 aud 模块，无法在内部播放（可改用系统播放器打开缓存文件）。"
    return ""


def _load_sound(path: str):
    """兼容不同版本的 ``Sound.file`` 签名。

    实测（Blender 5.2）：``Sound.file(path, True)``（cached=True）对 mp3/m4a 这类
    压缩格式会报 “couldn't be read with any installed file reader”，而只传路径
    的写法三种格式都能放，所以默认走流式读取。
    """
    attempts = (
        lambda: _aud.Sound.file(path),
        lambda: _aud.Sound(path),
        lambda: _aud.Sound.file(path, True),
    )
    last_error = None
    for attempt in attempts:
        try:
            return attempt()
        except TypeError as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    raise RuntimeError("无法加载音频")


class AudioEngine:
    """一次只播一首歌的简易播放器。"""

    def __init__(self, volume: float = 0.8):
        self._device = None
        self._handle = None
        self._sound = None
        self._path = ""
        self._volume = max(0.0, min(1.0, float(volume)))
        self._paused = False
        self._started = 0.0
        self._expected_duration = 0.0
        self.last_error = ""
        self.loop = False

    # ------------------------------------------------------------ 设备

    def ensure_device(self) -> bool:
        if _aud is None:
            self.last_error = aud_error()
            return False
        if self._device is None:
            try:
                self._device = _aud.Device()
                self._device.volume = self._volume
            except Exception as exc:  # noqa: BLE001
                self.last_error = "打开音频设备失败：%s" % exc
                self._device = None
                return False
        return True

    @property
    def device(self):
        return self._device

    # ------------------------------------------------------------ 播放

    def play_file(self, path: str, expected_duration: float = 0.0) -> bool:
        """播放本地音频文件（mp3 / m4a / flac / wav …）。

        ``expected_duration`` 是接口给出的歌曲时长（秒），解码器拿不到长度时用它兜底。
        """
        if not self.ensure_device():
            return False
        if not path or not os.path.isfile(path):
            self.last_error = "音频文件不存在：%s" % path
            return False
        self.stop()
        try:
            sound = _load_sound(path)
        except Exception as exc:  # noqa: BLE001
            self.last_error = "无法解码该音频（%s）：%s" % (os.path.basename(path), exc)
            return False
        try:
            handle = self._device.play(sound)
        except Exception as exc:  # noqa: BLE001
            self.last_error = "播放失败：%s" % exc
            return False
        try:
            handle.volume = self._volume
            handle.loop_count = -1 if self.loop else 0
        except Exception:  # noqa: BLE001
            pass
        self._sound = sound
        self._handle = handle
        self._path = path
        self._paused = False
        self._started = time.time()
        self._expected_duration = max(0.0, float(expected_duration or 0.0))
        self.last_error = ""
        return True

    def stop(self):
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.stop()
            except Exception:  # noqa: BLE001
                pass
        self._sound = None
        self._paused = False
        self._started = 0.0
        self._expected_duration = 0.0

    def pause(self) -> bool:
        if self._handle is None:
            return False
        try:
            self._handle.pause()
            self._paused = True
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = "暂停失败：%s" % exc
            return False

    def resume(self) -> bool:
        if self._handle is None:
            return False
        try:
            self._handle.resume()
            self._paused = False
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = "继续播放失败：%s" % exc
            return False

    # ------------------------------------------------------------ 状态

    def _raw_status(self):
        if self._handle is None:
            return None
        try:
            return self._handle.status
        except Exception:  # noqa: BLE001
            return None

    @property
    def is_active(self) -> bool:
        """声音还在播（含暂停）。"""
        raw = self._raw_status()
        if raw is None:
            return False
        if isinstance(raw, bool):
            return raw
        return raw in (_STATUS_PLAYING, _STATUS_PAUSED)

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_playing(self) -> bool:
        return self._handle is not None and not self._paused and self.is_active

    @property
    def is_paused(self) -> bool:
        return self._handle is not None and self._paused

    @property
    def finished(self) -> bool:
        """自然播放结束（``stop()`` 之后 ``_handle`` 已置空，不会误判）。"""
        if self._handle is None:
            return False
        if time.time() - self._started < 0.6:
            return False
        raw = self._raw_status()
        if raw is None:
            return False
        if isinstance(raw, bool):
            return not raw
        return raw == _STATUS_STOPPED

    @property
    def position(self) -> float:
        if self._handle is None:
            return 0.0
        try:
            return float(self._handle.position)
        except Exception:  # noqa: BLE001
            return 0.0

    @property
    def duration(self) -> float:
        """秒。优先用解码出来的真实长度，其次用接口给的元数据时长。"""
        decoded = 0.0
        if self._sound is not None:
            try:
                length = float(self._sound.length)
                rate = 0.0
                try:
                    specs = self._sound.specs
                    if isinstance(specs, (tuple, list)) and specs:
                        rate = float(specs[0])
                except Exception:  # noqa: BLE001
                    rate = 0.0
                if length > 0:
                    decoded = length / rate if rate > 1.0 else length / 44100.0
            except Exception:  # noqa: BLE001
                decoded = 0.0
        if 1.0 < decoded < 3 * 3600:
            return decoded
        return self._expected_duration or decoded

    def seek(self, seconds: float) -> bool:
        if self._handle is None:
            return False
        target = max(0.0, float(seconds))
        duration = self.duration
        if duration > 0:
            target = min(target, max(0.0, duration - 0.2))
        try:
            self._handle.position = target
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = "跳转失败：%s" % exc
            return False

    # ------------------------------------------------------------ 音量

    @property
    def volume(self) -> float:
        return self._volume

    def set_volume(self, value: float):
        self._volume = max(0.0, min(1.0, float(value)))
        if self._handle is not None:
            try:
                self._handle.volume = self._volume
            except Exception:  # noqa: BLE001
                pass
        if self._device is not None:
            try:
                self._device.volume = self._volume
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------
# 下载 / 缓存
# --------------------------------------------------------------------------


def cache_path(cache_dir: str, song_id: str, quality: str, url: str = "") -> str:
    ext = QUALITY_EXT.get(quality, "")
    if not ext and url:
        ext = os.path.splitext(url.split("?")[0])[1].lstrip(".").lower()
    ext = ext or "mp3"
    return os.path.join(cache_dir, "%s.%s" % (utils.clean_filename(song_id, 40), ext))


def download(url: str, dest: str, progress=None, timeout: float = 30.0) -> str:
    """下载到 ``dest``（先写 ``.part`` 再改名），``progress`` 收到 (已下载, 总大小)。"""
    utils.ensure_dir(os.path.dirname(dest))
    part = dest + ".part"
    headers = {
        "User-Agent": utils.USER_AGENT,
        "Referer": "https://music.163.com/",
        "Accept": "*/*",
    }
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=utils._context()) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(part, "wb") as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    except urllib.error.URLError as exc:
        _cleanup(part)
        raise utils.HttpError("下载音频失败：%s" % getattr(exc, "reason", exc), None, url)
    except Exception:
        _cleanup(part)
        raise
    if os.path.getsize(part) <= 0:
        _cleanup(part)
        raise utils.HttpError("下载到的音频是空文件", None, url)
    if os.path.exists(dest):
        _cleanup(dest)
    os.replace(part, dest)
    return dest


def _cleanup(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def clear_cache(cache_dir: str) -> tuple:
    """清空缓存目录，返回 (删除文件数, 释放字节数)。"""
    count = 0
    size = 0
    if not os.path.isdir(cache_dir):
        return 0, 0
    for name in os.listdir(cache_dir):
        path = os.path.join(cache_dir, name)
        if os.path.isfile(path):
            try:
                size += os.path.getsize(path)
                os.remove(path)
                count += 1
            except OSError:
                pass
    return count, size


def cache_size(cache_dir: str) -> tuple:
    count = 0
    size = 0
    if not os.path.isdir(cache_dir):
        return 0, 0
    for name in os.listdir(cache_dir):
        path = os.path.join(cache_dir, name)
        if os.path.isfile(path):
            count += 1
            try:
                size += os.path.getsize(path)
            except OSError:
                pass
    return count, size


def open_in_system_player(path: str) -> bool:
    """aud 不可用时的退路：交给系统默认播放器 / 文件管理器。"""
    if not path or not os.path.exists(path):
        return False
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", path])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception as exc:  # noqa: BLE001
        utils.log("调用系统程序失败：%s" % exc)
        return False


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return "%.1f %s" % (num, unit)
        num /= 1024.0
    return "%.1f GB" % num


def elapsed_text(seconds: float) -> str:
    return utils.format_duration(seconds * 1000.0)


def now_ms() -> int:
    return int(time.time() * 1000)
