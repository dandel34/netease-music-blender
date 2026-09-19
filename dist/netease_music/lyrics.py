# -*- coding: utf-8 -*-
"""LRC 歌词解析与时间轴查询（纯标准库，可在 Blender 外做单元测试）。

网易云的歌词接口返回标准 LRC：

* ``[mm:ss.xx]歌词``  —— 一行可能带多个时间戳（重复副歌）
* ``[offset:±毫秒]``  —— 整首歌的时间偏移
* ``[ti:]``/``[ar:]`` 等元信息行需要忽略
* 翻译放在另一个字段（``tlyric``），时间戳与原文可能差几毫秒

浮层每 0.5 秒取一次“当前行”，所以查询用的是二分而不是遍历。
"""

from __future__ import annotations

import bisect
import re

#: ``[01:23.45]`` / ``[01:23:450]`` / ``[01:23]``
TAG_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
OFFSET_RE = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.I)
META_RE = re.compile(r"^\[(ar|ti|al|by|re|ve|length|kana)\s*:.*\]$", re.I)

#: 原文与翻译的时间戳容差（秒）
TRANSLATION_TOLERANCE = 0.35


def _to_seconds(minutes: str, seconds: str, fraction: str) -> float:
    value = int(minutes) * 60 + int(seconds)
    if fraction:
        value += int(fraction) / (10 ** len(fraction))
    return float(value)


def parse_lrc(text: str) -> list:
    """解析 LRC，返回按时间升序的 ``[(秒, 歌词), ...]``。"""
    offset = 0.0
    entries = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = OFFSET_RE.match(line)
        if match:
            # LRC 约定：offset 为正表示歌词整体提前
            offset = int(match.group(1)) / 1000.0
            continue
        if META_RE.match(line):
            continue
        stamps = TAG_RE.findall(line)
        if not stamps:
            continue
        body = re.sub(r"\s+", " ", TAG_RE.sub("", line)).strip()
        if not body:
            continue
        for minutes, seconds, fraction in stamps:
            entries.append((_to_seconds(minutes, seconds, fraction) - offset, body))
    entries.sort(key=lambda item: item[0])
    return entries


def build_timeline(lrc_text: str, translated_text: str = "") -> list:
    """原文 + 翻译合成时间轴：``[{"time": 秒, "text": 原文, "tr": 翻译}, ...]``。"""
    original = parse_lrc(lrc_text)
    translations = parse_lrc(translated_text)
    timeline = []
    used = set()
    for time_value, body in original:
        translation = ""
        best = None
        for index, (trans_time, trans_body) in enumerate(translations):
            if index in used:
                continue
            delta = abs(trans_time - time_value)
            if delta <= TRANSLATION_TOLERANCE and (best is None or delta < best[0]):
                best = (delta, index, trans_body)
        if best:
            used.add(best[1])
            translation = best[2]
        timeline.append({"time": time_value, "text": body, "tr": translation})
    return timeline


def counts_for(total: int) -> tuple:
    """把「显示几行」换算成 ``(当前行之上几行, 当前行之下几行)``。

    当前句永远居中：1 行 → (0, 0)；3 行 → (1, 1)；5 行 → (2, 2)。
    偶数会被就近取整（4 行 → (2, 1)）。
    """
    try:
        total = max(1, int(total))
    except (TypeError, ValueError):
        total = 1
    above = total // 2
    return above, total - 1 - above


def index_at(timeline, position: float) -> int:
    """``position``（秒）对应的歌词下标；还没唱到第一句时返回 ``-1``。"""
    if not timeline:
        return -1
    try:
        position = max(0.0, float(position))
    except (TypeError, ValueError):
        position = 0.0
    times = [item["time"] for item in timeline]
    return bisect.bisect_right(times, position) - 1


def window(timeline, position: float, above: int = 1, below: int = 2) -> dict:
    """取当前行与上下文，供浮层绘制。

    返回 ``{"index": 当前行, "items": [(相对行号, 歌词项), ...]}``。
    """
    index = index_at(timeline, position)
    first = max(0, index - above)
    last = min(len(timeline), index + below + 1)
    return {"index": index, "items": [(i - index, timeline[i]) for i in range(first, last)]}


def current_text(timeline, position: float) -> tuple:
    """``(当前行原文, 当前行翻译, 下一行原文)``，用于面板显示。"""
    if not timeline:
        return "", "", ""
    index = index_at(timeline, position)
    current = timeline[index] if 0 <= index < len(timeline) else None
    following = timeline[index + 1] if 0 <= index + 1 < len(timeline) else None
    return (
        current["text"] if current else "",
        (current.get("tr") or "") if current else "",
        following["text"] if following else "",
    )


def plain_text(timeline, limit: int = 0) -> str:
    lines = [item["text"] for item in timeline]
    if limit:
        lines = lines[:limit]
    return "\n".join(lines)
