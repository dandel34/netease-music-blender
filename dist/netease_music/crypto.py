# -*- coding: utf-8 -*-
"""网易云音乐请求加密（纯标准库实现，无第三方依赖）。

网易云音乐的 Web 端 / 移动端接口对请求体做了两层处理：

* **weapi**（``https://music.163.com/weapi/...``）
  1. 明文 JSON 用固定密钥 ``0CoJUm6Qyw8W8jud`` 做 AES-128-CBC（IV 固定）；
  2. 上一步结果再用一个随机 16 字符串做第二次 AES-128-CBC；
  3. 随机密钥倒序后当作大整数做 RSA 加密（e=0x10001，模数固定），
     结果补齐为 256 位十六进制 → ``encSecKey``。
  表单字段：``params``（base64）、``encSecKey``。

* **eapi**（``https://interface.music.163.com/eapi/...``，App 端接口）
  把 ``nobody{url}use{json}md5forencrypt`` 取 MD5，拼成
  ``{url}-36cd479b6b5-{json}-36cd479b6b5-{digest}`` 后用固定密钥
  ``e82ckenh8dichen8`` 做 AES-128-ECB，输出大写十六进制 → ``params``。

* **linuxapi**（``/api/linux/forward``）同 ECB，密钥 ``rFgB&h#%2?^eDg:Q``。

本模块只做“把请求体算出来”，不保存任何账号凭据；AES 为纯 Python 实现，
因此插件在 Blender 内置解释器里也能直接跑（无需 pycryptodome）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import string
import time

__all__ = [
    "aes_cbc_encrypt",
    "aes_ecb_encrypt",
    "weapi_params",
    "eapi_params",
    "linuxapi_params",
    "pkcs7_pad",
    "pkcs7_unpad",
    "selftest",
]

# --------------------------------------------------------------------------
# 协议常量（公开协议复刻，见 NeteaseCloudMusicApi / pyncm 等实现）
# --------------------------------------------------------------------------

PRESET_KEY = b"0CoJUm6Qyw8W8jud"
IV = b"0102030405060708"
EAPI_KEY = b"e82ckenh8dichen8"
LINUXAPI_KEY = b"rFgB&h#%2?^eDg:Q"
EAPI_SEPARATOR = "-36cd479b6b5-"

#: 网易云音乐 Web 端 RSA 公钥（官方前端内置）。模数由 PEM 解析得出，
#: 避免手抄 256 位十六进制常量时打错字符。
WEAPI_PUBLIC_KEY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDgtQn2JZ34ZC28NWYpAUd98iZ37BUrX/aKzmFbt7clFSs6"
    "sXqHauqKWqdtLkF2KexO40H1YTX8z2lSgBBOAxLsvaklV8k4cBFK9snQXE9/DDaFt6Rr7iVZMldczhC0JNgTz"
    "+SHXT6CBHuX3e9SdB1Ua44oncaTWz7OBGLbCiK45wIDAQAB\n"
    "-----END PUBLIC KEY-----"
)
SECKEY_ALPHABET = string.ascii_letters + string.digits


def _der_read(data: bytes, offset: int):
    """极简 DER TLV 读取：返回 (tag, 内容起止, 下一个偏移)。"""
    tag = data[offset]
    offset += 1
    length = data[offset]
    offset += 1
    if length & 0x80:  # 长格式长度
        count = length & 0x7F
        length = int.from_bytes(data[offset:offset + count], "big")
        offset += count
    return tag, offset, offset + length


def _parse_rsa_public_key(pem: str):
    """从 SubjectPublicKeyInfo PEM 里取出 (模数, 指数) 的整数形式。"""
    body = "".join(line for line in pem.splitlines() if not line.startswith("-----"))
    der = base64.b64decode(body)
    _, seq_start, _ = _der_read(der, 0)                      # 外层 SEQUENCE
    tag, alg_start, alg_end = _der_read(der, seq_start)      # AlgorithmIdentifier
    tag, bit_start, bit_end = _der_read(der, alg_end)        # BIT STRING
    inner = der[bit_start + 1:bit_end]                       # 去掉未用位数那个字节
    _, rsa_start, _ = _der_read(inner, 0)                    # RSAPublicKey SEQUENCE
    tag, mod_start, mod_end = _der_read(inner, rsa_start)    # INTEGER n
    tag, exp_start, exp_end = _der_read(inner, mod_end)      # INTEGER e
    modulus = int.from_bytes(inner[mod_start:mod_end], "big")
    exponent = int.from_bytes(inner[exp_start:exp_end], "big")
    return modulus, exponent


WEAPI_MODULUS_INT, WEAPI_PUBKEY_INT = _parse_rsa_public_key(WEAPI_PUBLIC_KEY_PEM)
#: 保持十六进制字符串形态，便于自检（官方常量为 256 位十六进制）。
WEAPI_MODULUS = format(WEAPI_MODULUS_INT, "x").zfill(256)
WEAPI_PUBKEY = format(WEAPI_PUBKEY_INT, "x")

# --------------------------------------------------------------------------
# AES-128：S 盒由有限域运算生成，避免手抄 256 项表出错
# --------------------------------------------------------------------------


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a = (a ^ 0x1B) & 0xFF
    return a


def _gmul(a: int, b: int) -> int:
    """GF(2^8) 乘法，模多项式 x^8+x^4+x^3+x+1 (0x11B)。"""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        b >>= 1
        a = _xtime(a)
    return p


def _gf_inv(a: int) -> int:
    """GF(2^8) 乘法逆元：a^254（a=0 时约定为 0）。"""
    if a == 0:
        return 0
    result, base, exp = 1, a, 254
    while exp:
        if exp & 1:
            result = _gmul(result, base)
        base = _gmul(base, base)
        exp >>= 1
    return result


def _build_sbox():
    sbox = [0] * 256
    for i in range(256):
        inv = _gf_inv(i)
        s = inv
        for shift in (1, 2, 3, 4):
            s ^= ((inv << shift) | (inv >> (8 - shift))) & 0xFF
        sbox[i] = (s ^ 0x63) & 0xFF
    return sbox


SBOX = _build_sbox()
MUL2 = [_xtime(i) for i in range(256)]
MUL3 = [_xtime(i) ^ i for i in range(256)]
RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _expand_key(key: bytes):
    """AES-128 密钥扩展 → 11 个 16 字节轮密钥（与状态同序，列优先）。"""
    if len(key) != 16:
        raise ValueError("AES-128 需要 16 字节密钥，收到 %d" % len(key))
    words = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[b] for b in temp]
            temp[0] ^= RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    round_keys = []
    for r in range(11):
        rk = bytearray(16)
        for c in range(4):
            w = words[r * 4 + c]
            for row in range(4):
                rk[c * 4 + row] = w[row]
        round_keys.append(bytes(rk))
    return round_keys


def _encrypt_block(block: bytes, round_keys) -> bytearray:
    s = bytearray(block)
    rk0 = round_keys[0]
    for i in range(16):
        s[i] ^= rk0[i]
    for rnd in range(1, 10):
        # SubBytes + ShiftRows（行 r 左移 r）
        t = bytearray(16)
        for row in range(4):
            for col in range(4):
                t[col * 4 + row] = SBOX[s[((col + row) % 4) * 4 + row]]
        # MixColumns
        for col in range(4):
            i = col * 4
            a0, a1, a2, a3 = t[i], t[i + 1], t[i + 2], t[i + 3]
            s[i] = MUL2[a0] ^ MUL3[a1] ^ a2 ^ a3
            s[i + 1] = a0 ^ MUL2[a1] ^ MUL3[a2] ^ a3
            s[i + 2] = a0 ^ a1 ^ MUL2[a2] ^ MUL3[a3]
            s[i + 3] = MUL3[a0] ^ a1 ^ a2 ^ MUL2[a3]
        rk = round_keys[rnd]
        for i in range(16):
            s[i] ^= rk[i]
    # 最后一轮：不做 MixColumns
    t = bytearray(16)
    for row in range(4):
        for col in range(4):
            t[col * 4 + row] = SBOX[s[((col + row) % 4) * 4 + row]]
    rk = round_keys[10]
    for i in range(16):
        t[i] ^= rk[i]
    return t


def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad = block_size - (len(data) % block_size)
    return data + bytes([pad]) * pad


def pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def aes_ecb_encrypt(data: bytes, key: bytes, pad: bool = True) -> bytes:
    """AES-128-ECB 加密（eapi / linuxapi 用）。"""
    round_keys = _expand_key(key)
    buf = pkcs7_pad(data) if pad else data
    if len(buf) % 16:
        raise ValueError("ECB 数据长度必须是 16 的倍数")
    out = bytearray()
    for off in range(0, len(buf), 16):
        out += _encrypt_block(buf[off:off + 16], round_keys)
    return bytes(out)


def aes_cbc_encrypt(data: bytes, key: bytes, iv: bytes = IV) -> bytes:
    """AES-128-CBC 加密（weapi 用）。"""
    if len(iv) != 16:
        raise ValueError("IV 需要 16 字节")
    round_keys = _expand_key(key)
    buf = pkcs7_pad(data)
    out = bytearray()
    prev = iv
    for off in range(0, len(buf), 16):
        block = bytes(a ^ b for a, b in zip(buf[off:off + 16], prev))
        enc = bytes(_encrypt_block(block, round_keys))
        out += enc
        prev = enc
    return bytes(out)


# --------------------------------------------------------------------------
# 三种协议的请求体构造
# --------------------------------------------------------------------------


def _rsa_encrypt_hex(text: str) -> str:
    """RSA 加密随机密钥：倒序 → 十六进制 → 模幂 → 256 位十六进制。"""
    raw = text[::-1].encode("utf-8")
    value = pow(int(binascii.hexlify(raw), 16), WEAPI_PUBKEY_INT, WEAPI_MODULUS_INT)
    return format(value, "x").zfill(256)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _dumps(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def weapi_params(payload, random_key: str | None = None) -> dict:
    """返回 ``{"params": ..., "encSecKey": ...}``。"""
    secret = random_key or "".join(secrets.choice(SECKEY_ALPHABET) for _ in range(16))
    first = aes_cbc_encrypt(_dumps(payload), PRESET_KEY, IV)
    second = aes_cbc_encrypt(first, secret.encode("utf-8"), IV)
    return {"params": _b64(second), "encSecKey": _rsa_encrypt_hex(secret)}


def eapi_params(url: str, payload) -> dict:
    """eapi：返回 ``{"params": 大写十六进制}``。``url`` 需为 ``/api/xxx`` 形式。"""
    text = _dumps(payload).decode("utf-8")
    message = "nobody%suse%smd5forencrypt" % (url, text)
    digest = hashlib.md5(message.encode("utf-8")).hexdigest()
    data = "%s%s%s%s%s" % (url, EAPI_SEPARATOR, text, EAPI_SEPARATOR, digest)
    return {"params": binascii.hexlify(aes_ecb_encrypt(data.encode("utf-8"), EAPI_KEY)).decode().upper()}


def linuxapi_params(url: str, payload) -> dict:
    body = {"method": "POST", "url": url, "params": payload}
    return {"eparams": binascii.hexlify(aes_ecb_encrypt(_dumps(body), LINUXAPI_KEY)).decode().upper()}


def header_params(url: str, payload) -> dict:
    """eapi 的 ``header`` 变体（部分 App 接口要求参数放在 Header 里）。"""
    text = _dumps(payload).decode("utf-8")
    digest = hashlib.md5(("nobody%suse%smd5forencrypt" % (url, text)).encode("utf-8")).hexdigest()
    data = "%s%s%s%s%s" % (url, EAPI_SEPARATOR, text, EAPI_SEPARATOR, digest)
    return {"params": binascii.hexlify(aes_ecb_encrypt(data.encode("utf-8"), EAPI_KEY)).decode().upper()}


# --------------------------------------------------------------------------
# 自检（FIPS-197 / NIST SP 800-38A 官方测试向量）
# --------------------------------------------------------------------------


def selftest() -> dict:
    """跑一遍权威测试向量，返回 ``{项目: (是否通过, 说明)}``。"""
    report = {}

    def check(name, got, want):
        ok = got == want
        report[name] = (ok, "ok" if ok else "got %r want %r" % (got, want))

    # FIPS-197 C.1：AES-128 单分组
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    check(
        "FIPS-197 AES-128 ECB",
        aes_ecb_encrypt(plain, key, pad=False).hex(),
        "69c4e0d86a7b0430d8cdb78070b4c55a",
    )
    # FIPS-197 C.1 第二轮：全零密钥 / 全零明文
    check(
        "FIPS-197 AES-128 zero",
        aes_ecb_encrypt(bytes(16), bytes(16), pad=False).hex(),
        "66e94bd4ef8a2c3b884cfa59ca342b2e",
    )
    # NIST SP 800-38A F.2.1：CBC 第一分组（本实现会自动补 PKCS#7，故只比首块）
    cbc_key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    cbc_iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    check(
        "NIST SP800-38A AES-128-CBC",
        aes_cbc_encrypt(bytes.fromhex("6bc1bee22e409f96e93d7e117393172a"), cbc_key, cbc_iv)[:16].hex(),
        "7649abac8119b246cee98e9b12e9197d",
    )
    # PKCS#7 填充
    check("PKCS#7 pad", pkcs7_pad(b"abc").hex(), "6162630d0d0d0d0d0d0d0d0d0d0d0d0d")
    check("PKCS#7 unpad", pkcs7_unpad(pkcs7_pad(b"hello")), b"hello")
    # 协议常量自检
    check("weapi 模数长度", len(WEAPI_MODULUS), 256)
    params = weapi_params({"a": 1}, random_key="0123456789abcdef")
    check("weapi encSecKey 长度", len(params["encSecKey"]), 256)
    check("weapi params 为 base64", _b64(base64.b64decode(params["params"])) == params["params"], True)
    # 两次调用必须产生不同密钥（随机性）
    k1 = weapi_params({"a": 1})["encSecKey"]
    k2 = weapi_params({"a": 1})["encSecKey"]
    check("weapi 随机密钥", k1 != k2, True)
    # eapi 参数为偶数长度的大写十六进制
    eparams = eapi_params("/api/test", {"a": 1})["params"]
    check(
        "eapi params 大写十六进制",
        all(c in "0123456789ABCDEF" for c in eparams) and len(eparams) % 32 == 0,
        True,
    )
    return report


if __name__ == "__main__":  # pragma: no cover
    failed = 0
    for name, (ok, detail) in selftest().items():
        print("[%s] %s: %s" % ("PASS" if ok else "FAIL", name, detail))
        failed += 0 if ok else 1
    print("time", time.strftime("%Y-%m-%d %H:%M:%S"))
    raise SystemExit(1 if failed else 0)
