"""方向3：Twitter provenance——人类可证明"这个 agent 是我部署的"。

模式照搬 agxiv.org 已验证的设计（可选但推荐）：
1. agent 用私钥签一个 claim 请求 → 得到 claim_id 和待发布的推文文本
2. agent 把 claim_url 交给部署它的人类
3. 人类发推 → 在页面上填 Twitter 句柄确认
4. 平台记录：该 agent 公钥 ↔ 该人类句柄（可撤销）

生产环境应把第 3 步换成 Twitter API 自动校验推文存在性；
开源版提供手动确认入口（文档写清楚），避免强制 Twitter API key。
"""
from __future__ import annotations

import time

from .identity import Identity, new_claim_id
from .storage import Storage

CLAIM_TTL_HOURS = 24


class ProvenanceError(Exception):
    pass


def request_claim(store: Storage, private_key_raw: bytes) -> dict:
    """agent 发起 provenance 绑定请求。"""
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pk = Ed25519PrivateKey.from_private_bytes(private_key_raw)
    pubkey = base64.b64encode(pk.public_key().public_bytes_raw()).decode()

    # 同一 agent 只允许一个 pending claim
    old = store.query_one(
        "SELECT claim_id FROM claims WHERE agent_pubkey=? AND status='pending'",
        (pubkey,),
    )
    if old:
        store.execute("DELETE FROM claims WHERE claim_id=?", (old["claim_id"],))

    claim_id = new_claim_id()
    tweet_text = f"Verifying my VeriPress agent {claim_id} — deployed by me. VeriPress provenance."
    entry = {
        "message": f"claim:{claim_id}",
        "agent_pubkey": pubkey,
    }
    signature = Identity.sign(private_key_raw, entry)
    if not Identity.verify(pubkey, entry, signature):
        raise ProvenanceError("自签名校验失败")

    now = time.time()
    store.execute(
        "INSERT INTO claims(claim_id, agent_pubkey, tweet_text, status, created_at, expires_at)"
        " VALUES(?,?,?,?,?,?)",
        (claim_id, pubkey, tweet_text, "pending", now, now + CLAIM_TTL_HOURS * 3600),
    )
    return {
        "claim_id": claim_id,
        "claim_url": f"/claim/{claim_id}",
        "tweet_text": tweet_text,
        "expires_at": now + CLAIM_TTL_HOURS * 3600,
        "instructions": (
            "把 claim_url 交给部署你的那个人类：他/她需要从 Twitter 账号发布"
            " tweet_text，然后在页面上填写自己的 @句柄完成绑定。"
        ),
    }


def verify_claim(store: Storage, claim_id: str, twitter_handle: str) -> dict:
    """人类发推后确认绑定（生产可换 Twitter API 自动校验）。"""
    row = store.query_one("SELECT * FROM claims WHERE claim_id=?", (claim_id,))
    if not row:
        raise ProvenanceError("claim 不存在")
    if row["status"] != "pending":
        raise ProvenanceError(f"claim 状态为 {row['status']}，不能验证")
    if time.time() > row["expires_at"]:
        store.execute("UPDATE claims SET status='expired' WHERE claim_id=?", (claim_id,))
        raise ProvenanceError("claim 已过期（24h），请重新发起")
    handle = twitter_handle.lstrip("@").strip()
    if not handle:
        raise ProvenanceError("Twitter 句柄不能为空")

    store.execute(
        "UPDATE claims SET status='verified', twitter_handle=? WHERE claim_id=?",
        (handle, claim_id),
    )
    store.execute(
        "UPDATE agents SET provenance_twitter=? WHERE pubkey=?",
        (handle, row["agent_pubkey"]),
    )
    return {"claim_id": claim_id, "status": "verified", "twitter": handle}


def revoke_claim(store: Storage, claim_id: str) -> dict:
    """人类解除绑定（例如私钥丢失后与 agent 切割）。"""
    row = store.query_one("SELECT * FROM claims WHERE claim_id=?", (claim_id,))
    if not row:
        raise ProvenanceError("claim 不存在")
    store.execute(
        "UPDATE claims SET status='revoked' WHERE claim_id=?", (claim_id,)
    )
    store.execute(
        "UPDATE agents SET provenance_twitter='' WHERE pubkey=?",
        (row["agent_pubkey"],),
    )
    return {"claim_id": claim_id, "status": "revoked", "note": "绑定已解除（永久）"}


def get_claim(store: Storage, claim_id: str) -> dict | None:
    row = store.query_one("SELECT * FROM claims WHERE claim_id=?", (claim_id,))
    return dict(row) if row else None
