"""合规对账报告（企业付费功能）。

审计部门要的不是"我们的账是对的"，而是**一份能独立验算、带平台签名的证明**。
这里输出的报告：

1. 统计窗口内的业务量（文档/版本/赏金/结算）；
2. 账本快照（流通量、escrow 总额、不变量结果）；
3. 审计链状态（条数、链头哈希、是否完整）；
4. 用平台 Ed25519 私钥对 `sha256(canonical(body))` 签名。

验证方只需持有 `veripress/compliance_public.pem`，就能确认这份报告
确实由该平台生成、且内容未被改动。

私钥来源（按优先级）：
- 环境变量 VERIPRESS_COMPLIANCE_KEY（base64 的 32 字节种子）
- 文件 keys/compliance.key（hex 或 base64，gitignored）
两者都没有时，报告照常生成但标记 `signed: false`——不假装签过。
"""
from __future__ import annotations

import base64
import hashlib
import os
import time
from pathlib import Path

from . import audit, ledger
from .canonical import canonical
from .storage import Storage

_KEY_FILE = Path(__file__).resolve().parent.parent / "keys" / "compliance.key"
_CWD_KEY_FILE = Path("keys/compliance.key")
_ENV = "VERIPRESS_COMPLIANCE_KEY"


def _load_private_key():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except Exception:  # pragma: no cover
        return None
    raw = os.environ.get(_ENV, "").strip()
    if raw:
        try:
            return Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw))
        except Exception:
            return None
    candidate = _KEY_FILE if _KEY_FILE.exists() else (_CWD_KEY_FILE if _CWD_KEY_FILE.exists() else None)
    if candidate is not None:
        try:
            txt = candidate.read_text().strip()
            try:
                seed = bytes.fromhex(txt)
            except ValueError:
                seed = base64.b64decode(txt)
            return Ed25519PrivateKey.from_private_bytes(seed)
        except Exception:
            return None
    return None


def build_report(store: Storage, start: float | None = None, end: float | None = None) -> dict:
    end = time.time() if end is None else end
    start = (end - 30 * 86400) if start is None else start

    def count(sql: str, params: tuple = ()) -> int:
        return store.query_one(sql, params)["n"]

    def window_count(table: str, col: str = "created_at") -> int:
        return count(f"SELECT COUNT(*) AS n FROM {table} WHERE {col}>=? AND {col}<=?",
                     (start, end))

    escrow_total = 0
    for r in store.query_all(
        "SELECT credit AS acct, SUM(amount) AS bal FROM ledger_entries"
        " WHERE credit LIKE 'escrow:%' GROUP BY credit"
    ):
        debit = store.query_one(
            "SELECT COALESCE(SUM(amount),0) AS n FROM ledger_entries WHERE debit=?",
            (r["acct"],),
        )["n"]
        escrow_total += r["bal"] - debit

    settled = count(
        "SELECT COUNT(*) AS n FROM ledger_entries WHERE kind='payout' AND created_at>=? AND created_at<=?",
        (start, end))

    body = {
        "report_version": 1,
        "generated_at": end,
        "period": {"start": start, "end": end},
        "activity": {
            "documents_published": window_count("documents"),
            "versions_created": window_count("versions"),
            "bounties_created": window_count("bounties"),
            "bounties_settled": settled,
            "comments": window_count("comments"),
            "endorsements": window_count("endorsements", "created_at"),
        },
        "ledger": {
            "currency": "VP",
            "supply": -ledger.balance(store, ledger.SYSTEM),
            "escrow_outstanding": escrow_total,
            "entries": count("SELECT COUNT(*) AS n FROM ledger_entries"),
            "invariant_ok": ledger.check_invariant(store),
        },
        "audit": {
            "entries": count("SELECT COUNT(*) AS n FROM audit_log"),
            "chain": audit.verify_chain(store),
            "head_hash": audit.last_hash(store),
        },
        "totals": {
            "agents": count("SELECT COUNT(*) AS n FROM agents"),
            "research_objects": count("SELECT COUNT(*) AS n FROM research_objects"),
        },
    }

    # 先定下 signed 标志再算摘要：验签方是从最终 JSON 反推的，
    # 摘要里少一个字段就对不上（踩过一次）。
    key = _load_private_key()
    body["signed"] = key is not None

    digest = hashlib.sha256(canonical(body)).hexdigest()
    body["digest_sha256"] = digest

    if key is None:
        body["signature"] = ""
        body["note"] = "未配置合规签名私钥（VERIPRESS_COMPLIANCE_KEY 或 keys/compliance.key）"
        return body

    body["signature"] = base64.b64encode(key.sign(canonical(body))).decode()
    try:
        body["public_key_fingerprint"] = hashlib.sha256(
            key.public_key().public_bytes_raw()).hexdigest()[:16]
    except Exception:  # pragma: no cover
        pass
    return body


def verify_report(report: dict, public_key_bytes: bytes | None = None) -> bool:
    """验证方独立验签：报告内容 + 签名即可，不需要访问数据库。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    sig = report.get("signature") or ""
    if not sig:
        return False
    body = {k: v for k, v in report.items()
            if k not in ("signature", "public_key_fingerprint")}
    core = {k: v for k, v in body.items() if k != "digest_sha256"}
    if hashlib.sha256(canonical(core)).hexdigest() != body.get("digest_sha256"):
        return False
    if public_key_bytes is None:
        pem = Path(__file__).parent / "compliance_public.pem"
        if not pem.exists():
            return False
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        pub = load_pem_public_key(pem.read_bytes())
    else:
        pub = Ed25519PublicKey.from_public_bytes(public_key_bytes)
    try:
        pub.verify(base64.b64decode(sig), canonical(body))
        return True
    except Exception:
        return False
