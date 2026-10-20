"""方向2：双式账本——赏金托管的地基。

从 aria-agent 移植并简化。核心不变量：
  每一笔 post(debit, credit, amount) 同时记入两侧，全局 sum(amount) == 0。
账户余额 = 贷方发生额 - 借方发生额。

为什么赏金托管必须用复式账本而不是"钱包字段"：
托管中的钱既不属于发布者也不属于平台，它是一个"在途负债"。
单式记账（改字段）无法表达这种三方关系，也无法审计。
"""
from __future__ import annotations

from .storage import Storage

SYSTEM = "system"
INITIAL_GRANT = 1000  # 新 agent / 人类的启动赠款（VP）


def escrow_account(bounty_id: str) -> str:
    return f"escrow:{bounty_id}"


def agent_account(pubkey: str) -> str:
    return f"agent:{pubkey}"


def post(
    store: Storage,
    debit: str,
    credit: str,
    amount: int,
    kind: str,
    ref: str = "",
    note: str = "",
) -> int:
    """记一笔账。amount > 0。返回 entry id。"""
    if amount <= 0:
        raise ValueError("金额必须为正")
    if debit == credit:
        raise ValueError("借贷方不能相同")
    cur = store.execute(
        "INSERT INTO ledger_entries(debit, credit, amount, kind, ref, note, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (debit, credit, amount, kind, ref, note, Storage.now()),
    )
    return cur.lastrowid


def balance(store: Storage, account: str) -> int:
    row = store.query_one(
        "SELECT"
        " (SELECT COALESCE(SUM(amount),0) FROM ledger_entries WHERE credit=?)"
        " - (SELECT COALESCE(SUM(amount),0) FROM ledger_entries WHERE debit=?) AS bal",
        (account, account),
    )
    return row["bal"]


def check_invariant(store: Storage) -> bool:
    """复式记账核心不变量：所有账户余额之和 == 0。

    每一笔 post 让借方账户 -amount、贷方账户 +amount，
    所以任何时刻 Σ(所有账户余额) 必须为 0。
    （system 余额 = -(流通量)，escrow 是负债中转，agents/humans 是资产。）
    """
    row = store.query_one(
        "SELECT COALESCE(SUM(bal),0) AS net FROM ("
        "  SELECT SUM(amount) AS bal FROM ledger_entries GROUP BY credit"
        "  UNION ALL"
        "  SELECT -SUM(amount) AS bal FROM ledger_entries GROUP BY debit)"
    )
    return row["net"] == 0


def ensure_grant(store: Storage, pubkey: str) -> int:
    """首次见到某 agent 时赠启动金（幂等）。"""
    acct = agent_account(pubkey)
    row = store.query_one(
        "SELECT COUNT(*) AS n FROM ledger_entries WHERE kind='grant' AND credit=?", (acct,)
    )
    if row["n"] == 0:
        post(store, SYSTEM, acct, INITIAL_GRANT, "grant", ref=pubkey, note="启动赠款")
    return balance(store, acct)


def ensure_human_grant(store: Storage, handle: str) -> int:
    """人类账户同样有启动赠款（幂等）。"""
    acct = human_account(handle)
    row = store.query_one(
        "SELECT COUNT(*) AS n FROM ledger_entries WHERE kind='grant' AND credit=?", (acct,)
    )
    if row["n"] == 0:
        post(store, SYSTEM, acct, INITIAL_GRANT, "grant", ref=handle, note="人类启动赠款")
    return balance(store, acct)


def human_account(handle: str) -> str:
    return f"human:{handle}"


def bounty_funds_total(store: Storage, bounty_id: str) -> dict:
    """某个赏金的资金构成：发起托管 + 人类加注。"""
    esc = escrow_account(bounty_id)
    parts = {r["kind"]: r["total"] for r in store.query_all(
        "SELECT kind, COALESCE(SUM(amount),0) AS total FROM ledger_entries"
        " WHERE credit=? AND kind IN ('escrow','bounty_fund') GROUP BY kind",
        (esc,),
    )}
    return {
        "bounty": parts.get("escrow", 0),
        "human_funding": parts.get("bounty_fund", 0),
        "total": parts.get("escrow", 0) + parts.get("bounty_fund", 0),
    }


def flow(store: Storage, account: str, limit: int = 50) -> list[dict]:
    """某账户的流水（收入/支出双向）。"""
    rows = store.query_all(
        "SELECT * FROM ledger_entries WHERE debit=? OR credit=? ORDER BY id DESC LIMIT ?",
        (account, account, limit),
    )
    out = []
    for r in rows:
        direction = "in" if r["credit"] == account else "out"
        counterpart = r["debit"] if direction == "in" else r["credit"]
        out.append(
            {
                "id": r["id"],
                "direction": direction,
                "counterpart": counterpart,
                "amount": r["amount"],
                "kind": r["kind"],
                "ref": r["ref"],
                "note": r["note"],
                "created_at": r["created_at"],
            }
        )
    return out
