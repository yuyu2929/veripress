"""签名请求 / 重放防护 / 限流 / 角色。

为什么这是企业采购的硬门槛：
现在 API 让客户端把**私钥**放在请求体里发给服务端代签。自托管小团队无所谓，
但高校或企业研究院的安全评审一定打回："私钥不得离开密钥库，更不得过网"。

所以这里提供 second path：客户端本地签名，服务端只收
`{pubkey, signature, nonce, timestamp}` + 原始请求体，
用公钥验签，私钥永不过网。签名覆盖 method + path + body 哈希 + nonce + ts，
所以请求体、路径、时间任一被改都验不过，nonce 复用会被拒。

服务端代签保留给本地开发，生产环境由 VERIPRESS_ENV=production 关掉。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time

from .canonical import canonical
from .identity import Identity
from .storage import Storage

MAX_CLOCK_SKEW = 300          # 秒；超过即拒绝（防重放窗口）
NONCE_TTL = 3600              # nonce 保留时长
RATE_WINDOW = 60              # 限流窗口（秒）
RATE_LIMIT = int(os.environ.get("VERIPRESS_RATE_LIMIT", "240"))  # 每窗口每 IP 写请求数


class AuthError(Exception):
    pass


def allow_server_signing() -> bool:
    """生产环境默认禁止"私钥过网"模式。"""
    explicit = os.environ.get("VERIPRESS_ALLOW_SERVER_SIGNING")
    if explicit is not None:
        return explicit.lower() in ("1", "true", "yes", "on")
    return os.environ.get("VERIPRESS_ENV", "dev").lower() != "production"


def admin_key() -> str:
    return os.environ.get("VERIPRESS_ADMIN_KEY", "")


def is_admin(supplied: str | None) -> bool:
    key = admin_key()
    return bool(key) and bool(supplied) and supplied == key


def signed_payload(method: str, path: str, body: bytes, nonce: str, ts: float) -> dict:
    """签名覆盖的规范内容——服务端与客户端必须逐字一致。"""
    return {
        "method": method.upper(),
        "path": path,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "nonce": nonce,
        "ts": round(float(ts), 3),
    }


def verify_signed_request(
    store: Storage,
    method: str,
    path: str,
    body: bytes,
    pubkey: str,
    signature: str,
    nonce: str,
    ts: float,
    now: float | None = None,
) -> str:
    """校验签名请求，返回 actor（pubkey）。失败抛 AuthError。"""
    now = time.time() if now is None else now
    if not nonce or len(nonce) < 8:
        raise AuthError("nonce 缺失或过短")
    if abs(now - float(ts)) > MAX_CLOCK_SKEW:
        raise AuthError(f"时间戳偏差超过 {MAX_CLOCK_SKEW} 秒")

    payload = signed_payload(method, path, body, nonce, ts)
    if not Identity.verify(pubkey, payload, signature):
        raise AuthError("签名校验失败")

    # nonce 一次性：同一个 nonce 第二次出现直接拒绝（重放）
    row = store.query_one("SELECT pubkey FROM used_nonces WHERE nonce=?", (nonce,))
    if row:
        raise AuthError("nonce 已被使用（重放请求）")
    store.execute(
        "INSERT INTO used_nonces(nonce, pubkey, seen_at) VALUES(?,?,?)",
        (nonce, pubkey, now),
    )
    # 顺手清理过期 nonce（不需要精确，够用即可）
    if int(now) % 30 == 0:
        store.execute("DELETE FROM used_nonces WHERE seen_at<?", (now - NONCE_TTL,))
    return pubkey


_rate: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


def check_rate(client: str) -> None:
    """按客户端做窗口限流；超限抛 AuthError(429 语义)。"""
    now = time.time()
    with _rate_lock:
        bucket = [t for t in _rate.get(client, []) if now - t < RATE_WINDOW]
        if len(bucket) >= RATE_LIMIT:
            raise AuthError(f"请求过于频繁：{RATE_WINDOW} 秒内上限 {RATE_LIMIT} 次")
        bucket.append(now)
        _rate[client] = bucket


def reset_rate() -> None:
    with _rate_lock:
        _rate.clear()
