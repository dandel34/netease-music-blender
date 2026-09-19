# -*- coding: utf-8 -*-
"""加密与接口连通性自检（可在 Blender 外直接用 CPython 运行）。

用法::

    python tools/test_netease_crypto.py                 # 离线测试向量
    python tools/test_netease_crypto.py --online        # 追加真实接口连通性测试

在线部分只访问**匿名可用**的接口，用来判断“加密算法 + 协议格式”是否被服务端接受。
报告写成 UTF-8 JSON，避免 Windows 控制台编码干扰。
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from netease_music import crypto  # noqa: E402
from netease_music import utils  # noqa: E402

REPORT = os.path.join(HERE, "..", ".tmp", "online_report.json")


def offline():
    print("=== 离线测试向量 ===")
    failed = 0
    for name, (ok, detail) in crypto.selftest().items():
        print("  [%s] %-32s %s" % ("PASS" if ok else "FAIL", name, detail))
        failed += 0 if ok else 1
    print("  公钥指数 = 0x%s，模数十六进制长度 = %d"
          % (crypto.WEAPI_PUBKEY, len(crypto.WEAPI_MODULUS)))
    return failed


def _trim(value, limit=60):
    text = str(value)
    return text[:limit]


def online():
    print("=== 在线接口连通性（匿名） ===")
    base = "https://music.163.com"
    cases = [
        ("登录二维码 key", "/weapi/login/qr/key", {}, lambda d: (d.get("data") or {}).get("unikey")),
        ("登录状态", "/weapi/w/nuser/account/get", {}, lambda d: d.get("account")),
        ("搜索", "/weapi/search/get", {"s": "海阔天空", "type": 1, "limit": 1, "offset": 0},
         lambda d: (d.get("result", {}).get("songs") or [{}])[0].get("name")),
        ("歌曲直链", "/weapi/song/enhance/player/url/v1",
         {"ids": "[347230]", "level": "exhigh", "encodeType": "aac"},
         lambda d: ((d.get("data") or [{}])[0] or {}).get("url") or (d.get("data") or [{}])[0].get("fee")),
        ("每日推荐(需登录)", "/weapi/v3/discovery/recommend/songs", {"offset": 0, "limit": 3, "total": True},
         lambda d: len(((d.get("data") or {}).get("dailySongs") or []))),
        ("首页 block page(私人雷达来源)", "/weapi/homepage/block/page",
         {"refresh": True, "cursor": 0}, lambda d: len(((d.get("data") or {}).get("blocks") or []))),
    ]
    report = {}
    for label, path, payload, pick in cases:
        entry = {"path": path}
        try:
            data = utils.http_post_form(base + path, crypto.weapi_params(payload), timeout=20, retries=1)
            entry["code"] = data.get("code")
            try:
                entry["pick"] = _trim(pick(data))
            except Exception as exc:  # noqa: BLE001
                entry["pick"] = "解析失败 %s" % exc
            ok = data.get("code") == 200
            print("  [%s] %-28s code=%-6s %s" % ("PASS" if ok else "WARN", label, entry["code"], entry["pick"]))
        except Exception as exc:  # noqa: BLE001
            entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            print("  [FAIL] %-28s %s" % (label, entry["error"]))
        report[label] = entry

    # 二维码图片：面板里直接显示服务端给的 PNG，避免自己实现 QR 编码
    try:
        key_data = utils.http_post_form(base + "/weapi/login/qr/key", crypto.weapi_params({}), timeout=20)
        unikey = (key_data.get("data") or {}).get("unikey")
        created = utils.http_post_form(
            base + "/weapi/login/qr/create",
            crypto.weapi_params({"key": unikey, "qrimg": True, "type": 1}),
            timeout=20,
        )
        qrimg = (created.get("data") or {}).get("qrimg") or ""
        qrurl = (created.get("data") or {}).get("qrurl") or ""
        ok = qrimg.startswith("data:image/png;base64,")
        print("  [%s] %-28s qrimg=%d 字节  qrurl=%s" % ("PASS" if ok else "WARN", "二维码图片", len(qrimg), qrurl))
        report["二维码图片"] = {"qrimg_len": len(qrimg), "qrurl": qrurl}
    except Exception as exc:  # noqa: BLE001
        print("  [FAIL] %-28s %s" % ("二维码图片", exc))
        report["二维码图片"] = {"error": str(exc)}

    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print("报告已写入 %s" % os.path.normpath(REPORT))
    return 0


if __name__ == "__main__":
    rc = offline()
    if "--online" in sys.argv:
        rc += online()
    raise SystemExit(1 if rc else 0)
