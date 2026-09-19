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
    """加载音频文件（兼容不同版本的 ``Sound.file`` 签名）。

    实测（Blender 5.2）：`Sound.file(path, True)` 这种多传一个参数的写法对 mp3/m4a
    会报 “couldn't be read with any installed file reader”，正确用法是只传路径，
    需要整段读进内存时再调用 :meth:`Sound.cache`。
    """
    attempts = (
        lambda: _aud.Sound.file(path),
        lambda: _aud.Sound(path),
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


#: 解码进内存后大约每分鐘占多少内存（实测：float32 双声道 44.1kHz ≈ 20MB/分钟）
RAM_PER_MINUTE_MB = 20.0
#: 超过这个时长就不做内存缓存了（避免一首长混音吃掉几百 MB）
RAM_CACHE_MAX_MINUTES = 15.0

#: 音频后端候选（不同平台支持的类型不同，创建失败会自动退回默认后端）
BACKEND_ITEMS = [
    ("auto", "自动（跟随 Blender）", "用 Blender 自己配置的音频后端，最省心"),
    ("WASAPI", "WASAPI（Windows）", "Windows 默认后端；CPU 满载时可能出现爆音，可换 OpenAL 试试"),
    ("OpenAL", "OpenAL（CPU 满载更稳）", "独立混音线程 + 大环形缓冲，渲染时更抗卡顿"),
    ("SDL", "SDL", "部分平台可用"),
    ("PulseAudio", "PulseAudio（Linux）", "Linux 常用后端"),
    ("ALSA", "ALSA（Linux）", "Linux 底层后端"),
]

#: 音频缓冲候选（帧数 → 毫秒在 44.1kHz 下约等于帧数/44.1）
BUFFER_ITEMS = [
    ("1024", "1024 帧 ≈ 23ms（低延迟）", "默认值，CPU 满载时最容易爆音"),
    ("2048", "2048 帧 ≈ 46ms", ""),
    ("4096", "4096 帧 ≈ 93ms", ""),
    ("8192", "8192 帧 ≈ 186ms（推荐，抗卡顿）", "给混音线程更多余量，代价是起播略慢一点"),
    ("16384", "16384 帧 ≈ 372ms（最稳）", ""),
    ("32768", "32768 帧 ≈ 743ms", ""),
]


def sound_seconds(sound) -> float:
    """音频时长（秒）；拿不到时返回 0。"""
    try:
        length = float(sound.length)
        specs = sound.specs
        rate = float(specs[0]) if isinstance(specs, (tuple, list)) and specs else 0.0
        return length / rate if rate > 1.0 else length / 44100.0
    except Exception:  # noqa: BLE001
        return 0.0


class AudioEngine:
    """一次只播一首歌的简易播放器。

    两条抗卡顿的措施都在设备/加载这一层：

    * ``buffer_frames``：``aud.Device`` 的缓冲帧数。实测默认只有 1024 帧（≈23ms），
      在 Cycles 之类把 CPU 打满的场景下，混音线程一旦错过这个很短的期限就会爆音；
      加大到 8192 帧（≈186ms）余量充足得多。
    * ``ram_cache``：用 ``Sound.cache()`` 把整段音频解码进内存，这样播放回调里
      不再有“读磁盘 + 解码”，只剩混音本身。代价约 20MB/分钟。
    """

    def __init__(self, volume: float = 0.8, backend: str = "auto",
                 buffer_frames: int = 8192, ram_cache: bool = True):
        self._device = None
        self._device_key = None
        self._handle = None
        self._sound = None
        self._path = ""
        self._volume = max(0.0, min(1.0, float(volume)))
        self._paused = False
        self._started = 0.0
        self._expected_duration = 0.0
        self.last_error = ""
        self.loop = False
        self.backend = backend or "auto"
        self.buffer_frames = int(buffer_frames or 8192)
        self.ram_cache = bool(ram_cache)
        self.last_device = ""

    # ------------------------------------------------------------ 设备

    def _device_kwargs(self, backend: str):
        name = "" if backend in ("", "auto") else backend
        return name

    def ensure_device(self, backend: str = None, buffer_frames: int = None) -> bool:
        if _aud is None:
            self.last_error = aud_error()
            return False
        if backend is not None:
            self.backend = backend or "auto"
        if buffer_frames is not None:
            self.buffer_frames = int(buffer_frames or 8192)
        key = (self.backend, self.buffer_frames)
        if self._device is not None and self._device_key == key:
            return True
        if self._device is not None and self._handle is not None:
            return True                      # 正在播就别换设备，等这一首放完
        self._device = None
        self._device_key = None
        name = self._device_kwargs(self.backend)
        candidates = [name]
        if name:
            candidates.append("")          # 指定的后端不可用时退回 Blender 默认后端
        attempts = []
        for candidate in candidates:
            if _aud is None:
                break
            attempts.append((candidate, 44100.0, 2, _aud.FORMAT_S16, self.buffer_frames, ""))
            attempts.append((candidate, 0.0, 0, _aud.FORMAT_S16, self.buffer_frames, ""))
        for args in attempts:
            try:
                device = _aud.Device(*args)
            except Exception as exc:  # noqa: BLE001
                self.last_error = "打开音频设备失败（%s）：%s" % (args[0] or "默认", exc)
                continue
            self._device = device
            self._device_key = key
            used = args[0] or "默认"
            self.last_device = "%s / 缓冲 %d 帧 / %s Hz / %d 声道" % (
                used, self.buffer_frames, device.rate, device.channels)
            if args[0] != name:
                utils.log("音频后端 %s 不可用，已退回默认后端" % (name or "默认"))
            try:
                device.volume = self._volume
            except Exception:  # noqa: BLE001
                pass
            return True
        self._device = None
        if not self.last_error:
            self.last_error = "打开音频设备失败"
        return False

    @property
    def device(self):
        return self._device

    # ------------------------------------------------------------ 播放

    def load_sound(self, path: str, ram_cache: bool = None):
        """加载音频，必要时用 ``Sound.cache()`` 把整段解码进内存。

        **这个方法可以在子线程里调用**：缓存一首 4 分钟的歌实测要 0.2～2 秒，
        放在主线程会卡界面，所以插件是在下载任务里顺手把解码做完的。
        """
        if not path or not os.path.isfile(path):
            raise FileNotFoundError("音频文件不存在：%s" % path)
        sound = _load_sound(path)
        use_cache = self.ram_cache if ram_cache is None else bool(ram_cache)
        if not use_cache:
            return sound
        seconds = sound_seconds(sound)
        if seconds and seconds > RAM_CACHE_MAX_MINUTES * 60.0:
            utils.log("音频长达 %.1f 分钟，跳过内存缓存（避免占用几百 MB）" % (seconds / 60.0))
            return sound
        try:
            cached = sound.cache()
            if cached is not None:
                utils.log_debug("已缓存进内存：%s（约 %.0f MB）"
                                % (os.path.basename(path), seconds / 60.0 * RAM_PER_MINUTE_MB))
                return cached
        except Exception as exc:  # noqa: BLE001
            utils.log("内存缓存失败，改用流式读取：%s" % exc)
        return sound

    def play_sound(self, sound, expected_duration: float = 0.0, path: str = "") -> bool:
        """播放一个已经加载好的 Sound 对象（主线程调用）。"""
        if not self.ensure_device():
            return False
        if sound is None:
            self.last_error = "没有可播放的音频"
            return False
        self.stop()
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
        self._path = path or ""
        self._paused = False
        self._started = time.time()
        self._expected_duration = max(0.0, float(expected_duration or 0.0))
        self.last_error = ""
        return True

    def play_file(self, path: str, expected_duration: float = 0.0, ram_cache: bool = None) -> bool:
        """播放本地音频文件（mp3 / m4a / flac / wav …）。

        ``expected_duration`` 是接口给出的歌曲时长（秒），解码器拿不到长度时用它兜底。
        """
        if not self.ensure_device():
            return False
        if not path or not os.path.isfile(path):
            self.last_error = "音频文件不存在：%s" % path
            return False
        try:
            sound = self.load_sound(path, ram_cache)
        except Exception as exc:  # noqa: BLE001
            self.last_error = "无法解码该音频（%s）：%s" % (os.path.basename(path), exc)
            return False
        return self.play_sound(sound, expected_duration, path)

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


def cache_files(cache_dir: str) -> list:
    """列出缓存里的音频文件：``[(路径, 修改时间, 大小), ...]``，按旧→新排序。"""
    items = []
    if not os.path.isdir(cache_dir):
        return items
    for name in os.listdir(cache_dir):
        path = os.path.join(cache_dir, name)
        if name.endswith(".part") or not os.path.isfile(path):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        items.append((path, stat.st_mtime, stat.st_size))
    items.sort(key=lambda item: item[1])
    return items


def touch(path: str) -> None:
    """把文件标记为“最近使用”，使缓存按 LRU 淘汰。"""
    try:
        os.utime(path, None)
    except OSError:
        pass


def _protected_paths(keep) -> set:
    """把 keep 归一化成绝对路径集合。

    注意要支持「单个路径字符串」和「路径序列」两种写法——字符串是可迭代的，
    如果直接 for 会把路径拆成一个个字符，保护就静默失效了（这个坑踩过）。
    """
    if not keep:
        return set()
    if isinstance(keep, (str, bytes, os.PathLike)):
        keep = [keep]
    return {os.path.abspath(path) for path in keep if path}


def enforce_cache_limit(cache_dir: str, limit: int, keep=()) -> tuple:
    """只保留最近使用的 ``limit`` 个音频文件，超出部分删掉旧的。

    ``keep`` 里的路径永不删除（正在播放的那一首），可传单个路径或路径序列。
    返回 ``(删除数量, 释放字节)``。
    """
    if not limit or limit <= 0:
        return 0, 0
    protected = _protected_paths(keep)
    items = [item for item in cache_files(cache_dir) if os.path.abspath(item[0]) not in protected]
    if len(items) <= limit:
        return 0, 0
    deleted = 0
    freed = 0
    for path, _mtime, size in items[:len(items) - limit]:
        try:
            os.remove(path)
            deleted += 1
            freed += size
        except OSError:
            pass
    return deleted, freed


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
    """统计已完成的缓存文件（与 cache_files 口径一致，不含 .part 临时文件）。"""
    count = 0
    size = 0
    if not os.path.isdir(cache_dir):
        return 0, 0
    for name in os.listdir(cache_dir):
        path = os.path.join(cache_dir, name)
        if name.endswith(".part") or not os.path.isfile(path):
            continue
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
