"""方向1：结构化研究模型——研究类型 + 里程碑 + Claim Cards。

对标 agentarxiv.org 已验证的设计（Research Objects），但开源化：
- 7 种研究类型：HYPOTHESIS / LITERATURE_SYNTHESIS / EXPERIMENT_PLAN /
  RESULT / REPLICATION_REPORT / BENCHMARK / NEGATIVE_RESULT
- 7 个里程碑（二元完成态）：claim_stated / assumptions_listed / test_plan /
  runnable_artifact / initial_results / independent_replication / conclusion_update
- Claim Card：可证伪声明 = claim + mechanism + prediction + falsifiable_by + confidence

设计原则（来自 AgentArxiv 的 best practices）：
不可证伪的声明降权——没有 falsifiable_by 的 HYPOTHESIS 不允许挂赏金。
"""
from __future__ import annotations

import secrets
import time

from .storage import Storage

RO_TYPES = (
    "HYPOTHESIS",
    "LITERATURE_SYNTHESIS",
    "EXPERIMENT_PLAN",
    "RESULT",
    "REPLICATION_REPORT",
    "BENCHMARK",
    "NEGATIVE_RESULT",
)

MILESTONES = (
    "claim_stated",
    "assumptions_listed",
    "test_plan",
    "runnable_artifact",
    "initial_results",
    "independent_replication",
    "conclusion_update",
)


class ResearchError(Exception):
    pass


def create_research_object(
    store: Storage,
    doc_id: str,
    ro_type: str,
    author_pubkey: str,
    claim: str = "",
    mechanism: str = "",
    prediction: str = "",
    falsifiable_by: str = "",
    confidence: float = 0.0,
) -> str:
    """把一篇论文升级为结构化研究对象。"""
    if ro_type not in RO_TYPES:
        raise ResearchError(f"未知研究类型: {ro_type}（可选: {', '.join(RO_TYPES)}）")
    if ro_type == "HYPOTHESIS" and not falsifiable_by:
        raise ResearchError("HYPOTHESIS 必须提供 falsifiable_by（可证伪标准）")
    if not 0.0 <= confidence <= 1.0:
        raise ResearchError("confidence 必须在 0~1 之间")
    if store.query_one("SELECT ro_id FROM research_objects WHERE doc_id=?", (doc_id,)):
        raise ResearchError("该论文已是研究对象")

    # ro_id 用 hex 随机：base64 公钥放进 URL 路径会被 '/' 截断（URL 安全陷阱）
    ro_id = "ro_" + secrets.token_hex(8)
    store.execute(
        "INSERT INTO research_objects(ro_id, doc_id, ro_type, claim, mechanism, prediction,"
        " falsifiable_by, confidence, milestones_json, author_pubkey, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            ro_id,
            doc_id,
            ro_type,
            claim,
            mechanism,
            prediction,
            falsifiable_by,
            confidence,
            "{}",
            author_pubkey,
            time.time(),
        ),
    )
    return ro_id


def get_research_object(store: Storage, ro_id: str) -> dict | None:
    row = store.query_one("SELECT * FROM research_objects WHERE ro_id=?", (ro_id,))
    if not row:
        return None
    import json

    d = dict(row)
    d["milestones"] = json.loads(d.pop("milestones_json"))
    d["progress"] = milestone_progress(d["milestones"])
    return d


def find_ro_by_doc(store: Storage, doc_id: str) -> dict | None:
    row = store.query_one(
        "SELECT ro_id FROM research_objects WHERE doc_id=?", (doc_id,)
    )
    return get_research_object(store, row["ro_id"]) if row else None


def update_milestone(store: Storage, ro_id: str, milestone: str, done: bool) -> dict:
    """推进里程碑。只有 independent_replication 需要外部证据，其余作者自填。"""
    if milestone not in MILESTONES:
        raise ResearchError(f"未知里程碑: {milestone}（可选: {', '.join(MILESTONES)}）")
    ro = get_research_object(store, ro_id)
    if not ro:
        raise ResearchError("研究对象不存在")
    import json

    ms = dict(ro["milestones"])
    ms[milestone] = bool(done)
    store.execute(
        "UPDATE research_objects SET milestones_json=? WHERE ro_id=?",
        (json.dumps(ms), ro_id),
    )
    return get_research_object(store, ro_id)


def milestone_progress(milestones: dict) -> float:
    done = sum(1 for m in MILESTONES if milestones.get(m))
    return round(done / len(MILESTONES), 3)


def claim_card(store: Storage, ro_id: str) -> dict | None:
    """Claim Card 展示数据（Web/CLI 用）。"""
    ro = get_research_object(store, ro_id)
    if not ro:
        return None
    return {
        "ro_id": ro_id,
        "type": ro["ro_type"],
        "claim": ro["claim"],
        "evidence": {
            "mechanism": ro["mechanism"],
            "prediction": ro["prediction"],
            "confidence": ro["confidence"],
            "falsifiable_by": ro["falsifiable_by"],
        },
        "milestones": {
            m: ro["milestones"].get(m, False) for m in MILESTONES
        },
        "progress": ro["progress"],
    }
