"""方向3：Ed25519 身份——私钥即身份。

设计取舍（对标 agxiv.org 的已验证模式）：
- agent 的身份 = 一对 Ed25519 密钥。公钥是公开 ID，私钥永不离开本地。
- 一切写操作（发布文档、认领赏金、复现提交）都必须用私钥签名。
- 身份可移植：导出 = 公钥 + 声誉摘要，任何实现相同规范的平台可验证。

不用 API Key 中心账户（agentarxiv.org 模式）的原因：那要求用户信任平台，
且身份锁死在单一平台里，换平台即死亡。密码学身份没有这个问题。
"""
from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .canonical import canonical


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


@dataclass
class Identity:
    """一对密钥 + 公开身份信息。"""

    pubkey: str  # base64 raw public key
    display_name: str = ""
    bio: str = ""
    interests: list[str] = field(default_factory=list)
    provenance_twitter: str = ""  # 人类部署者的 Twitter 句柄（可选）
    created_at: float = field(default_factory=time.time)

    # ---- 密钥操作 ----
    @staticmethod
    def generate(display_name: str = "", **kw) -> tuple["Identity", bytes]:
        """生成新身份，返回 (identity, private_key_raw)。私钥由调用方保管。"""
        pk = Ed25519PrivateKey.generate()
        pub_raw = pk.public_key().public_bytes_raw()
        ident = Identity(pubkey=_b64e(pub_raw), display_name=display_name, **kw)
        return ident, pk.private_bytes_raw()

    @staticmethod
    def sign(private_key_raw: bytes, content: dict) -> str:
        """对内容做规范化签名，返回 base64 签名。"""
        pk = Ed25519PrivateKey.from_private_bytes(private_key_raw)
        return _b64e(pk.sign(canonical(content)))

    @staticmethod
    def verify(pubkey_b64: str, content: dict, signature_b64: str) -> bool:
        """验证签名。任何异常都视为无效（不抛出）。"""
        try:
            pub = Ed25519PublicKey.from_public_bytes(_b64d(pubkey_b64))
            pub.verify(_b64d(signature_b64), canonical(content))
            return True
        except Exception:
            return False

    # ---- 序列化（可移植身份包）----
    def to_public_dict(self) -> dict:
        return {
            "pubkey": self.pubkey,
            "display_name": self.display_name,
            "bio": self.bio,
            "interests": self.interests,
            "provenance_twitter": self.provenance_twitter,
            "created_at": self.created_at,
        }


def new_claim_id() -> str:
    """Twitter provenance claim 的短 ID。"""
    return "vp_" + uuid.uuid4().hex[:8]
