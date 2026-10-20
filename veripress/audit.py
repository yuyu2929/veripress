"""审计日志：append-only 哈希链。

企业采购 VeriPress 时，合规部门问的第一个问题是
"谁在什么时候发了什么、动了哪笔钱"。没有这个，私有部署过不了审。

设计：
- 每条记录 hash = sha256(prev_hash || canonical(record_without_hash))
  于是任何一条被删改，其后所有 hash 都对不上——`verify_chain()` 一查即知。
- 记录本身只存元数据与 payload 的 sha256，不存正文（正文在 documents 表，
  这里要回答的是"谁做了这件事"，不是"内容是什么"）。
- 写审计与业务写操作应在同一个事务里（见 api 层用法），否则会出现
  "钱动了但审计没记"的空窗。
"""
from __future__ import annotations

import hashlib
import json
import time

from .canonical import canonical
from .storage import Storage

GENESIS = "0" * 64


def _entry_hash(prev_hash: str, record: dict) -> str:
    body = canonical(record)
    h = hashlib.sha256()
    h.update(prev_hash.encode("ascii"))
    h.update(body)
    return h.hexdigest()


def last_hash(store: Storage) -> str:
    row = store.query_one("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1")
    return row["hash"] if row else GENESIS


def append(
    store: Storage,
    actor: str,
    action: str,
    target: str = "",
    payload: dict | None = None,
    meta: dict | None = None,
) -> dict:
    """追加一条审计记录，返回写入的行（含 hash）。"""
    payload_sha = ""
    if payload is not None:
        payload_sha = hashlib.sha256(canonical(payload)).hexdigest()
    record = {
        "ts": round(time.time(), 6),
        "actor": actor,
        "action": action,
        "target": target,
        "payload_sha256": payload_sha,
        "meta": meta or {},
    }
    prev = last_hash(store)
    record["prev_hash"] = prev
    record["hash"] = _entry_hash(prev, record)
    store.execute(
        "INSERT INTO audit_log(ts, actor, action, target, payload_sha256,"
        " prev_hash, hash, meta_json) VALUES(?,?,?,?,?,?,?,?)",
        (
            record["ts"],
            record["actor"],
            record["action"],
            record["target"],
            record["payload_sha256"],
            record["prev_hash"],
            record["hash"],
            json.dumps(record["meta"], ensure_ascii=False, sort_keys=True),
        ),
    )
    store.execute(
        "UPDATE audit_head SET head_hash=?, length=length+1 WHERE id=1", (record["hash"],)
    )
    return record


def verify_chain(store: Storage) -> dict:
    """从头到尾重算整条链。返回 {'ok', 'length', 'broken_at', 'reason'}。"""
    rows = store.query_all("SELECT * FROM audit_log ORDER BY id ASC")
    prev = GENESIS
    for r in rows:
        record = {
            "ts": r["ts"],
            "actor": r["actor"],
            "action": r["action"],
            "target": r["target"],
            "payload_sha256": r["payload_sha256"],
            "meta": json.loads(r["meta_json"] or "{}"),
            "prev_hash": r["prev_hash"],
        }
        if r["prev_hash"] != prev:
            return {"ok": False, "length": len(rows), "broken_at": r["id"],
                    "reason": "prev_hash 与上一条不衔接"}
        if _entry_hash(prev, record) != r["hash"]:
            return {"ok": False, "length": len(rows), "broken_at": r["id"],
                    "reason": "记录内容被改动（hash 不匹配）"}
        prev = r["hash"]
    head = store.query_one("SELECT head_hash, length FROM audit_head WHERE id=1")
    if head is None:
        return {"ok": False, "length": len(rows), "broken_at": None,
                "reason": "缺少链头锚点（audit_head 被删）"}
    if head["length"] != len(rows):
        return {"ok": False, "length": len(rows), "broken_at": None,
                "reason": f"条数不符：锚点记录 {head['length']} 条，实际 {len(rows)} 条（末尾被删或插入）"}
    if head["head_hash"] != prev:
        return {"ok": False, "length": len(rows), "broken_at": None,
                "reason": "链头哈希与锚点不一致（末尾被篡改）"}
    return {"ok": True, "length": len(rows), "broken_at": None, "reason": ""}


def query(
    store: Storage,
    actor: str | None = None,
    action: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    sql = "SELECT * FROM audit_log"
    where, params = [], []
    if actor:
        where.append("actor=?")
        params.append(actor)
    if action:
        where.append("action LIKE ?")
        params.append(action + "%")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    rows = store.query_all(sql, tuple(params + [limit, offset]))
    out = []
    for r in rows:
        d = dict(r)
        d["meta"] = json.loads(d.pop("meta_json") or "{}")
        out.append(d)
    return out


def export_jsonl(store: Storage, limit: int = 100000) -> str:
    """导出为 JSONL（每行一条，含 hash 链），用于交给审计方独立验算。"""
    rows = store.query_all("SELECT * FROM audit_log ORDER BY id ASC LIMIT ?", (limit,))
    lines = []
    for r in rows:
        d = dict(r)
        d["meta"] = json.loads(d.pop("meta_json") or "{}")
        lines.append(json.dumps(d, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines)
