"""方向2：复现赏金——托管、认领、结算、退款。

全行业第一家把赏金资金流做成透明可审计的：
- 钱不进平台口袋，进复式账本的 escrow 账户（在途负债）
- 每个动作都是一笔账：托管 / 结算 / 退款，全局 sum==0 可验
- 反白嫖设计：复现报告提交后发布者 7 天内不裁决 → 自动结算给复现者
  （agentarxiv.org 的 bounty 是黑箱，发布者可以无限期拖着不付钱）

状态机：open → claimed → submitted → resolved / refunded
                          ↖（放弃）┘
"""
from __future__ import annotations

import secrets
import time

from . import audit, ledger
from .documents import publish
from .research import get_research_object
from .storage import Storage

# 允许挂赏金的研究类型（可复现的）
BOUNTYABLE = ("HYPOTHESIS", "RESULT", "BENCHMARK")
SETTLE_TIMEOUT_DAYS = 7


class BountyError(Exception):
    pass


def _pubkey_of(private_key_raw: bytes) -> str:
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pk = Ed25519PrivateKey.from_private_bytes(private_key_raw)
    return base64.b64encode(pk.public_key().public_bytes_raw()).decode()


def create_bounty(
    store: Storage,
    publisher_priv: bytes,
    ro_id: str,
    amount: int,
    description: str = "",
) -> dict:
    """发布者托管赏金。钱从发布者钱包划入 escrow 账户。"""
    ro = get_research_object(store, ro_id)
    if not ro:
        raise BountyError("研究对象不存在")
    if ro["ro_type"] not in BOUNTYABLE:
        raise BountyError(
            f"{ro['ro_type']} 类型不能挂赏金（可复现类型: {', '.join(BOUNTYABLE)}）"
        )
    if amount <= 0:
        raise BountyError("金额必须为正")

    pubkey = _pubkey_of(publisher_priv)
    bounty_id = "bn_" + secrets.token_hex(8)
    # 余额检查与记账必须在同一事务里：否则并发请求各自通过检查后
    # 各托管一笔，钱被凭空复制（复式账本仍平衡，不变量查不出来）。
    with store.transaction():
        ledger.ensure_grant(store, pubkey)
        acct = ledger.agent_account(pubkey)
        bal = ledger.balance(store, acct)
        if bal < amount:
            raise BountyError(f"余额不足：需要 {amount} VP，当前 {bal} VP")
        entry = ledger.post(
            store,
            acct,
            ledger.escrow_account(bounty_id),
            amount,
            "escrow",
            ref=bounty_id,
            note=f"赏金托管: {description[:80]}",
        )
        store.execute(
            "INSERT INTO bounties(bounty_id, ro_id, publisher_pubkey, amount, status,"
            " escrow_ref, resolution_note, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (bounty_id, ro_id, pubkey, amount, "open", str(entry), "", time.time()),
        )
        audit.append(store, pubkey, "bounty.create", bounty_id,
                     {"ro_id": ro_id, "amount": amount})
    return get_bounty(store, bounty_id)


def claim_bounty(store: Storage, claimer_priv: bytes, bounty_id: str) -> dict:
    """agent 认领赏金（= 承诺尝试复现）。"""
    b = get_bounty(store, bounty_id)
    if not b:
        raise BountyError("赏金不存在")
    if b["status"] != "open":
        raise BountyError(f"赏金当前状态 {b['status']}，不能认领")
    claimer = _pubkey_of(claimer_priv)
    if claimer == b["publisher_pubkey"]:
        raise BountyError("不能认领自己发布的赏金")
    ledger.ensure_grant(store, claimer)
    # CAS：带上 status='open' 条件。两个 agent 同时认领时只有一个 rowcount==1，
    # 另一个会拿到"已被认领"而不是静默覆盖对方的 claimer_pubkey。
    n = store.update(
        "UPDATE bounties SET status='claimed', claimer_pubkey=?"
        " WHERE bounty_id=? AND status='open'",
        (claimer, bounty_id),
    )
    if n != 1:
        raise BountyError("赏金已被他人认领（并发冲突）")
    audit.append(store, claimer, "bounty.claim", bounty_id)
    return get_bounty(store, bounty_id)


def abandon_bounty(store: Storage, claimer_priv: bytes, bounty_id: str) -> dict:
    """复现者放弃，赏金回到 open。"""
    b = get_bounty(store, bounty_id)
    if not b or b["status"] != "claimed":
        raise BountyError("只有 claimed 状态的赏金可以放弃")
    claimer = _pubkey_of(claimer_priv)
    if claimer != b["claimer_pubkey"]:
        raise BountyError("只有当前认领人可以放弃")
    n = store.update(
        "UPDATE bounties SET status='open', claimer_pubkey=''"
        " WHERE bounty_id=? AND status='claimed' AND claimer_pubkey=?",
        (bounty_id, claimer),
    )
    if n != 1:
        raise BountyError("赏金状态已变化（并发冲突）")
    audit.append(store, claimer, "bounty.abandon", bounty_id)
    return get_bounty(store, bounty_id)


def submit_replication(
    store: Storage,
    claimer_priv: bytes,
    bounty_id: str,
    content: dict,
) -> dict:
    """复现者提交复现报告（一篇签名文档），赏金进入 submitted。"""
    b = get_bounty(store, bounty_id)
    if not b or b["status"] != "claimed":
        raise BountyError("只有 claimed 状态的赏金可以提交复现报告")
    claimer = _pubkey_of(claimer_priv)
    if claimer != b["claimer_pubkey"]:
        raise BountyError("只有当前认领人可以提交复现报告")

    ro = get_research_object(store, b["ro_id"])
    content = dict(content)
    content["type"] = "replication"
    content.setdefault("tags", []).append("replication")
    content.setdefault("references", []).append(ro["doc_id"])
    content.setdefault("meta", {})["bounty_id"] = bounty_id

    # 先占位再发文档：CAS 抢到 submitted 的那个请求才继续写文档，
    # 否则（并发重复提交）直接报错，不产生第二篇复现论文。
    n = store.update(
        "UPDATE bounties SET status='submitted', submitted_at=?"
        " WHERE bounty_id=? AND status='claimed' AND claimer_pubkey=?",
        (time.time(), bounty_id, claimer),
    )
    if n != 1:
        raise BountyError("赏金状态已变化，无法提交（并发或已提交）")
    try:
        result = publish(store, claimer_priv, content)
    except Exception:
        # 发布失败则把状态退回 claimed，避免卡在 submitted 永远等自动结算
        store.update(
            "UPDATE bounties SET status='claimed', submitted_at=NULL"
            " WHERE bounty_id=? AND status='submitted'",
            (bounty_id,),
        )
        raise
    audit.append(store, claimer, "bounty.submit", bounty_id,
                 {"doc_id": result.get("doc_id", "")})
    out = get_bounty(store, bounty_id)
    out["replication_doc"] = result
    return out


def resolve_bounty(
    store: Storage,
    publisher_priv: bytes,
    bounty_id: str,
    accept: bool,
    note: str = "",
) -> dict:
    """发布者裁决：接受 → 结算给复现者；拒绝 → 退款给发布者。"""
    b = get_bounty(store, bounty_id)
    if not b or b["status"] != "submitted":
        raise BountyError("只有 submitted 状态的赏金可以裁决")
    publisher = _pubkey_of(publisher_priv)
    if publisher != b["publisher_pubkey"]:
        raise BountyError("只有发布者可以裁决")

    with store.transaction():
        n = store.update(
            "UPDATE bounties SET status=?, resolution_note=?, resolved_at=?"
            " WHERE bounty_id=? AND status='submitted' AND publisher_pubkey=?",
            ("resolved" if accept else "refunded", note, time.time(), bounty_id, publisher),
        )
        if n != 1:
            raise BountyError("赏金状态已变化（并发或已裁决）")
        settled_amount = _move_escrow(store, b, accept, note)
        audit.append(store, publisher, "bounty.resolve", bounty_id,
                     {"accept": accept, "amount": settled_amount, "note": note})

    if accept:
        store.execute(
            "UPDATE agents SET replication_score = replication_score + 1, karma = karma + 10"
            " WHERE pubkey=?",
            (b["claimer_pubkey"],),
        )
        store.execute(
            "UPDATE research_objects SET milestones_json = json_set(milestones_json,"
            " '$.independent_replication', 1) WHERE ro_id=?",
            (b["ro_id"],),
        )
    return get_bounty(store, bounty_id)


def _move_escrow(store: Storage, b: dict, accept: bool, note: str) -> int:
    """把 escrow 里的**全部余额**结清。

    为什么不用 b['amount']：人类加注（bounty_fund）也进 escrow，
    只结 amount 会把加注的钱永远锁在托管账户里 —— 账本平衡但钱卡死，
    是企业审计一眼就能看出来的坏账。
    返回实际结算金额。
    """
    esc = ledger.escrow_account(b["bounty_id"])
    amount = ledger.balance(store, esc)
    if amount <= 0:
        raise BountyError("托管余额为 0，无法结算（可能已结清）")
    if accept:
        ledger.post(store, esc, ledger.agent_account(b["claimer_pubkey"]), amount,
                    "payout", ref=b["bounty_id"], note=note or "复现被接受，结算")
    else:
        ledger.post(store, esc, ledger.agent_account(b["publisher_pubkey"]), amount,
                    "refund", ref=b["bounty_id"], note=note or "复现未被接受，退款")
    return amount


def settle_expired(store: Storage, now: float | None = None) -> list[dict]:
    """反白嫖：submitted 超过 SETTLE_TIMEOUT_DAYS 天仍未裁决 → 自动结算。

    这是**显式维护动作**（定时任务或 admin 调用），不再藏在 get_bounty() 里：
    读接口产生资金变动会让审计说不清"这笔钱是谁触发的"，
    而且并发读会重复结算。
    """
    now = time.time() if now is None else now
    cutoff = now - SETTLE_TIMEOUT_DAYS * 86400
    rows = store.query_all(
        "SELECT bounty_id FROM bounties WHERE status='submitted' AND submitted_at<=?",
        (cutoff,),
    )
    settled = []
    for r in rows:
        bid = r["bounty_id"]
        b = get_bounty(store, bid)
        if not b or b["status"] != "submitted":
            continue
        with store.transaction():
            n = store.update(
                "UPDATE bounties SET status='resolved', resolution_note=?, resolved_at=?"
                " WHERE bounty_id=? AND status='submitted'",
                (f"发布者 {SETTLE_TIMEOUT_DAYS} 天未裁决，自动结算", now, bid),
            )
            if n != 1:
                continue
            amount = _move_escrow(store, b, True, f"发布者 {SETTLE_TIMEOUT_DAYS} 天未裁决，自动结算")
            audit.append(store, "system", "bounty.auto_settle", bid, {"amount": amount})
        store.execute(
            "UPDATE agents SET replication_score = replication_score + 1, karma = karma + 10"
            " WHERE pubkey=?",
            (b["claimer_pubkey"],),
        )
        settled.append({"bounty_id": bid, "amount": amount, "claimer": b["claimer_pubkey"]})
    return settled


def _maybe_auto_settle(store: Storage, b: dict) -> dict:
    """只读版：告诉调用方还差几天自动结算，不做任何写入。"""
    if b["status"] != "submitted" or not b["submitted_at"]:
        return b
    age_days = (time.time() - b["submitted_at"]) / 86400
    b["auto_settle_in_days"] = round(max(0.0, SETTLE_TIMEOUT_DAYS - age_days), 1)
    return b


def get_bounty(store: Storage, bounty_id: str) -> dict | None:
    row = store.query_one("SELECT * FROM bounties WHERE bounty_id=?", (bounty_id,))
    if not row:
        return None
    b = dict(row)
    b["escrow_balance"] = ledger.balance(store, ledger.escrow_account(bounty_id))
    if b["submitted_at"]:
        b = _maybe_auto_settle(store, b)
    return b


def list_bounties(store: Storage, status: str | None = None, limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM bounties"
    params: tuple = ()
    if status:
        sql += " WHERE status=?"
        params = (status,)
    sql += " ORDER BY created_at DESC LIMIT ?"
    return [dict(r) for r in store.query_all(sql, params + (limit,))]


def replication_count(store: Storage, ro_id: str) -> int:
    """某研究的独立复现报告数（社区验证的原料）。"""
    ro = get_research_object(store, ro_id)
    row = store.query_one(
        "SELECT COUNT(DISTINCT v.signed_by) AS n FROM versions v"
        " WHERE json_extract(v.content_json,'$.type')='replication'"
        " AND EXISTS (SELECT 1 FROM json_each(json_extract(v.content_json,'$.references')) je"
        "             WHERE je.value=?)",
        (ro["doc_id"],),
    )
    return row["n"]
