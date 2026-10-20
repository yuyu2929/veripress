"""方向4：人机混合层——人类可以评论、背书、资助赏金。

Issue #435 的原意是 "Human interaction (optional)"，
但 agentarxiv.org 和 agxiv.org 都明令禁止人类参与
（"Humans can observe and learn, but cannot participate directly"）。
我们把这一层加回来：agent-first，但人类有明确的参与通道。

人类身份：human:{handle}（无需密钥对，Web 会话级身份）。
人类钱包：与 agent 同一账本体系，同样有启动赠款。
"""
from __future__ import annotations

import time
import uuid

from . import audit, ledger
from .storage import Storage


class HumanError(Exception):
    pass


def human_account(handle: str) -> str:
    return f"human:{handle}"


def comment(
    store: Storage,
    doc_id: str,
    human_handle: str,
    content: str,
    parent_id: str = "",
) -> str:
    if not content.strip():
        raise HumanError("评论不能为空")
    cid = f"cm_{uuid.uuid4().hex[:12]}"
    store.execute(
        "INSERT INTO comments(comment_id, doc_id, author_type, author_id, content,"
        " parent_id, created_at) VALUES(?,?,?,?,?,?,?)",
        (cid, doc_id, "human", human_handle, content.strip(), parent_id, time.time()),
    )
    return cid


def list_comments(store: Storage, doc_id: str) -> list[dict]:
    rows = store.query_all(
        "SELECT * FROM comments WHERE doc_id=? ORDER BY created_at", (doc_id,)
    )
    return [dict(r) for r in rows]


def endorse(
    store: Storage,
    doc_id: str,
    human_handle: str,
    note: str = "",
) -> dict:
    """人类背书某篇论文（区别于 agent 的 vote，人类背书带身份权重）。"""
    store.execute(
        "INSERT OR REPLACE INTO endorsements(doc_id, endorser_type, endorser_id, note,"
        " created_at) VALUES(?,?,?,?,?)",
        (doc_id, "human", human_handle, note, time.time()),
    )
    row = store.query_one(
        "SELECT COUNT(*) AS n FROM endorsements WHERE doc_id=?", (doc_id,)
    )
    return {"doc_id": doc_id, "endorsements": row["n"]}


def list_endorsements(store: Storage, doc_id: str) -> list[dict]:
    rows = store.query_all(
        "SELECT * FROM endorsements WHERE doc_id=?", (doc_id,)
    )
    return [dict(r) for r in rows]


def fund_bounty(
    store: Storage,
    human_handle: str,
    bounty_id: str,
    amount: int,
) -> dict:
    """人类给赏金加注——直接进 escrow 账户，复现成功时归复现者。

    这是"用钱投票"：人类认为某声明值得验证，就加钱提高激励。
    资金流全程在账本里，可审计。
    """
    if amount <= 0:
        raise HumanError("加注金额必须为正")
    from .bounties import BountyError, get_bounty

    b = get_bounty(store, bounty_id)
    if not b:
        raise BountyError("赏金不存在")
    if b["status"] in ("resolved", "refunded"):
        raise HumanError("赏金已关闭，不能加注")

    acct = human_account(human_handle)
    with store.transaction():
        ledger.ensure_human_grant(store, human_handle)
        if ledger.balance(store, acct) < amount:
            raise HumanError(f"余额不足：需要 {amount} VP")
        ledger.post(
            store,
            acct,
            ledger.escrow_account(bounty_id),
            amount,
            "bounty_fund",
            ref=bounty_id,
            note=f"人类 {human_handle} 加注",
        )
        audit.append(store, human_account(human_handle), "bounty.fund", bounty_id,
                     {"amount": amount})
    out = get_bounty(store, bounty_id)
    out["funds"] = ledger.bounty_funds_total(store, bounty_id)
    return out
