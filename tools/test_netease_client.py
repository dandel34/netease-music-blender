# -*- coding: utf-8 -*-
"""接口客户端连通性自检（匿名，不需要账号）。

    python tools/test_netease_client.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from netease_music import api  # noqa: E402


def main():
    report = {}
    client = api.NeteaseClient()
    print("协议优先级：", client._order())

    try:
        status = client.login_status()
        print("登录状态：", status)
        report["login_status"] = status
    except Exception as exc:  # noqa: BLE001
        print("登录状态查询失败：", exc)
        report["login_status"] = {"error": str(exc)}

    try:
        daily = client.daily_recommend()
        print("每日推荐：%d 首，例如 %s - %s" % (len(daily), daily[0]["name"], daily[0]["artists"]) if daily else "每日推荐：0 首")
        report["daily"] = {"count": len(daily), "first": daily[0] if daily else None}
    except Exception as exc:  # noqa: BLE001
        print("每日推荐失败：", exc)
        daily = []
        report["daily"] = {"error": str(exc)}

    try:
        detail = client.playlist_detail("3778678", track_limit=20)
        print("歌单详情：%s，%d 首（trackIds %d）" % (detail["name"], len(detail["tracks"]), len(detail["track_ids"])))
        report["playlist"] = {"name": detail["name"], "tracks": len(detail["tracks"]), "track_ids": len(detail["track_ids"])}
    except Exception as exc:  # noqa: BLE001
        print("歌单详情失败：", exc)
        report["playlist"] = {"error": str(exc)}

    # 播放直链：先用每日推荐里的第一首，拿不到就换一个已知可播的 id
    for song_id in ([daily[0]["id"]] if daily else []) + ["33894312"]:
        try:
            url = client.song_url(song_id, "exhigh")
            print("播放直链：song=%s level=%s br=%s type=%s url=%s..." % (song_id, url.get("requested_level"), url.get("br"), url.get("type"), url.get("url", "")[:48]))
            report["song_url"] = {"song": song_id, "level": url.get("requested_level"), "br": url.get("br"), "type": url.get("type")}
            break
        except Exception as exc:  # noqa: BLE001
            print("播放直链失败（song=%s）：%s" % (song_id, exc))
            report["song_url"] = {"error": str(exc)}

    try:
        payload = client.lyric(daily[0]["id"]) if daily else {}
        lrc_lines = [line for line in (payload.get("lrc") or "").splitlines() if line.strip()]
        print("歌词：%d 行 LRC，翻译 %d 行，例如 %s"
              % (len(lrc_lines), len((payload.get("translated") or "").splitlines()),
                 " | ".join(lrc_lines[:2])[:70]))
        report["lyric"] = {"lrc_lines": len(lrc_lines),
                           "has_translation": bool(payload.get("translated")),
                           "head": lrc_lines[:2]}
    except Exception as exc:  # noqa: BLE001
        print("歌词失败：", exc)
        report["lyric"] = {"error": str(exc)}

    try:
        res = client.recommend_resource()
        print("推荐歌单：%d 个" % len(res))
        report["recommend_resource"] = len(res)
    except Exception as exc:  # noqa: BLE001
        print("推荐歌单失败：", exc)
        report["recommend_resource"] = {"error": str(exc)}

    try:
        radar = client.find_radar_playlist()
        print("私人雷达：", radar)
        report["radar"] = radar
    except Exception as exc:  # noqa: BLE001
        print("私人雷达（匿名预期失败）：", str(exc)[:160])
        report["radar"] = {"error": str(exc)[:300]}

    print("--- 协议命中记录（最后 12 条）---")
    for transport, path, result in client.transport_log[-12:]:
        print("  %-6s %-42s %s" % (transport, path, result))
    report["transport_log"] = client.transport_log[-20:]

    out = os.path.join(HERE, "..", ".tmp", "client_report.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print("报告：", os.path.normpath(out))


if __name__ == "__main__":
    main()
