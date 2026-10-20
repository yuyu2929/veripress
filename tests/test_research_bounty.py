"""方向1 + 方向2 测试：结构化研究模型 + 赏金托管经济。"""
import time

import pytest

from veripress import bounties, documents, ledger, research
from veripress.identity import Identity
from veripress.research import ResearchError
from veripress.storage import Storage


@pytest.fixture
def store():
    return Storage(":memory:")


@pytest.fixture
def two_agents(store):
    a, pa = Identity.generate("Publisher")
    b, pb = Identity.generate("Replicator")
    for i in (a, b):
        documents.register_agent(store, i)
    ledger.ensure_grant(store, a.pubkey)
    ledger.ensure_grant(store, b.pubkey)
    return (a, pa), (b, pb)


@pytest.fixture
def ro_with_bounty(store, two_agents):
    (pub, pub_priv) = two_agents[0]
    r = documents.publish(store, pub_priv, {"type": "paper", "title": "RL boosts X",
                                            "body": "we claim RL boosts X"})
    ro_id = research.create_research_object(
        store, r["doc_id"], "HYPOTHESIS", pub.pubkey,
        claim="RL boosts X", mechanism="via Y", prediction="+15% on Z",
        falsifiable_by="no significant lift on Z", confidence=0.8)
    return r, ro_id


# ---------- 方向1 ----------

def test_create_research_object(store, ro_with_bounty):
    _, ro_id = ro_with_bounty
    ro = research.get_research_object(store, ro_id)
    assert ro["ro_type"] == "HYPOTHESIS"
    assert ro["claim"] == "RL boosts X"
    assert ro["progress"] == 0.0


def test_hypothesis_requires_falsifiable(store, two_agents):
    (pub, pub_priv) = two_agents[0]
    r = documents.publish(store, pub_priv, {"type": "paper", "title": "T", "body": "b"})
    with pytest.raises(ResearchError):
        research.create_research_object(store, r["doc_id"], "HYPOTHESIS", pub.pubkey,
                                        claim="不可证伪的断言")


def test_milestone_progress(store, ro_with_bounty):
    _, ro_id = ro_with_bounty
    research.update_milestone(store, ro_id, "claim_stated", True)
    research.update_milestone(store, ro_id, "test_plan", True)
    ro = research.get_research_object(store, ro_id)
    assert ro["progress"] == round(2 / 7, 3)


def test_claim_card(store, ro_with_bounty):
    _, ro_id = ro_with_bounty
    card = research.claim_card(store, ro_id)
    assert card["type"] == "HYPOTHESIS"
    assert card["evidence"]["falsifiable_by"] == "no significant lift on Z"
    assert set(card["milestones"]) == set(research.MILESTONES)


def test_invalid_confidence(store, two_agents, ro_with_bounty):
    with pytest.raises(ResearchError):
        research.update_milestone(store, ro_with_bounty[1], "nonexistent", True)


# ---------- 方向2 ----------

def test_bounty_escrow_flow(store, two_agents, ro_with_bounty):
    (pub, pub_priv) = two_agents[0]
    (rep, rep_priv) = two_agents[1]
    _, ro_id = ro_with_bounty

    b = bounties.create_bounty(store, pub_priv, ro_id, 500, "验证 RL boosts X")
    assert b["status"] == "open"
    assert b["escrow_balance"] == 500
    # 发布者钱少了
    assert ledger.balance(store, ledger.agent_account(pub.pubkey)) == ledger.INITIAL_GRANT - 500
    # 全局守恒
    assert ledger.check_invariant(store)

    b = bounties.claim_bounty(store, rep_priv, b["bounty_id"])
    assert b["status"] == "claimed" and b["claimer_pubkey"] == rep.pubkey

    b = bounties.submit_replication(store, rep_priv, b["bounty_id"],
                                    {"title": "复现报告", "body": "复现成功"})
    assert b["status"] == "submitted"
    assert b["replication_doc"]["doc_id"]

    b = bounties.resolve_bounty(store, pub_priv, b["bounty_id"], accept=True, note="靠谱")
    assert b["status"] == "resolved"
    # 钱到了复现者手里
    assert ledger.balance(store, ledger.agent_account(rep.pubkey)) == ledger.INITIAL_GRANT + 500
    # escrow 清零
    assert b["escrow_balance"] == 0
    assert ledger.check_invariant(store)
    # 声誉到账
    a = documents.get_agent(store, rep.pubkey)
    assert a["replication_score"] == 1 and a["karma"] == 10


def test_bounty_refund(store, two_agents, ro_with_bounty):
    (pub, pub_priv) = two_agents[0]
    (rep, rep_priv) = two_agents[1]
    _, ro_id = ro_with_bounty
    b = bounties.create_bounty(store, pub_priv, ro_id, 300)
    b = bounties.claim_bounty(store, rep_priv, b["bounty_id"])
    b = bounties.submit_replication(store, rep_priv, b["bounty_id"],
                                    {"title": "复现失败", "body": "没能复现"})
    b = bounties.resolve_bounty(store, pub_priv, b["bounty_id"], accept=False, note="复现无效")
    assert b["status"] == "refunded"
    assert ledger.balance(store, ledger.agent_account(pub.pubkey)) == ledger.INITIAL_GRANT
    assert ledger.check_invariant(store)


def test_insufficient_balance(store, two_agents, ro_with_bounty):
    (pub, pub_priv) = two_agents[0]
    _, ro_id = ro_with_bounty
    with pytest.raises(bounties.BountyError):
        bounties.create_bounty(store, pub_priv, ro_id, 99999)


def test_cannot_claim_own_bounty(store, two_agents, ro_with_bounty):
    (pub, pub_priv) = two_agents[0]
    _, ro_id = ro_with_bounty
    b = bounties.create_bounty(store, pub_priv, ro_id, 100)
    with pytest.raises(bounties.BountyError):
        bounties.claim_bounty(store, pub_priv, b["bounty_id"])


def test_auto_settle_anti_freeride(store, two_agents, ro_with_bounty):
    """反白嫖：提交复现后发布者 7 天不裁决 → 自动结算给复现者。"""
    (pub, pub_priv) = two_agents[0]
    (rep, rep_priv) = two_agents[1]
    _, ro_id = ro_with_bounty
    b = bounties.create_bounty(store, pub_priv, ro_id, 400)
    b = bounties.claim_bounty(store, rep_priv, b["bounty_id"])
    b = bounties.submit_replication(store, rep_priv, b["bounty_id"],
                                    {"title": "复现", "body": "ok"})
    # 把 submitted_at 改到 8 天前
    store.execute("UPDATE bounties SET submitted_at=? WHERE bounty_id=?",
                  (time.time() - 8 * 86400, b["bounty_id"]))
    # v0.2：自动结算改为显式维护动作（读接口不再产生资金变动）
    settled = bounties.settle_expired(store)
    assert [s["bounty_id"] for s in settled] == [b["bounty_id"]]
    b = bounties.get_bounty(store, b["bounty_id"])
    assert b["status"] == "resolved"
    assert ledger.balance(store, ledger.agent_account(rep.pubkey)) == ledger.INITIAL_GRANT + 400
    assert ledger.check_invariant(store)
    # 再跑一次必须幂等：不能二次结算
    assert bounties.settle_expired(store) == []


def test_non_bountyable_type(store, two_agents):
    (pub, pub_priv) = two_agents[0]
    r = documents.publish(store, pub_priv, {"type": "paper", "title": "综述", "body": "x"})
    ro_id = research.create_research_object(store, r["doc_id"], "LITERATURE_SYNTHESIS",
                                            pub.pubkey, claim="综述")
    with pytest.raises(bounties.BountyError):
        bounties.create_bounty(store, pub_priv, ro_id, 100)


def test_replication_count(store, two_agents, ro_with_bounty):
    (pub, pub_priv) = two_agents[0]
    (rep, rep_priv) = two_agents[1]
    r, ro_id = ro_with_bounty
    # 直接发一篇 replication 文档（不通过赏金）
    documents.publish(store, rep_priv, {"type": "replication", "title": "独立复现",
                                        "body": "confirmed", "references": [r["doc_id"]]})
    assert bounties.replication_count(store, ro_id) == 1


# ---------- 并发安全（v0.2 回归） ----------

def test_concurrent_bounty_cannot_overspend(store, two_agents, ro_with_bounty):
    """并发托管不能凭空造钱：余额 1000，20 个线程各托管 200，只能成功 5 个。

    修复前：每个线程都先读到 1000 >= 200，于是全部记账 → escrow 4000。
    复式账本依然平衡（Σ=0），不变量查不出来，但系统多发了 3000 VP。
    """
    import threading

    (pub, pub_priv) = two_agents[0]
    _, ro_id = ro_with_bounty
    results = []
    lock = threading.Lock()

    def worker():
        try:
            b = bounties.create_bounty(store, pub_priv, ro_id, 200)
            with lock:
                results.append(b["bounty_id"])
        except Exception:
            with lock:
                results.append(None)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    ok = [r for r in results if r]
    assert len(ok) == 5, f"应恰好成功 5 次，实际 {len(ok)}"
    assert ledger.balance(store, ledger.agent_account(pub.pubkey)) == 0
    assert ledger.check_invariant(store)


def test_concurrent_claim_only_one_wins(store, two_agents, ro_with_bounty):
    """两个 agent 同时认领同一赏金，只能有一个拿到。"""
    import threading

    (pub, pub_priv) = two_agents[0]
    (rep, rep_priv) = two_agents[1]
    _, ro_id = ro_with_bounty
    b = bounties.create_bounty(store, pub_priv, ro_id, 100)
    bid = b["bounty_id"]

    outcomes = []
    lock = threading.Lock()

    def claim(priv):
        try:
            bounties.claim_bounty(store, priv, bid)
            with lock:
                outcomes.append("ok")
        except Exception:
            with lock:
                outcomes.append("err")

    ts = [threading.Thread(target=claim, args=(rep_priv,)) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert outcomes.count("ok") == 1, f"应恰好 1 个成功，实际 {outcomes}"
    assert bounties.get_bounty(store, bid)["claimer_pubkey"] == rep.pubkey


def test_settle_pays_full_escrow_including_human_funding(store, two_agents, ro_with_bounty):
    """人类加注也进 escrow，结算必须把 escrow 结清，不能把加注钱锁死。"""
    from veripress import humans

    (pub, pub_priv) = two_agents[0]
    (rep, rep_priv) = two_agents[1]
    _, ro_id = ro_with_bounty
    b = bounties.create_bounty(store, pub_priv, ro_id, 300)
    bid = b["bounty_id"]
    humans.fund_bounty(store, "alice", bid, 250)   # 人类加注 250
    bounties.claim_bounty(store, rep_priv, bid)
    bounties.submit_replication(store, rep_priv, bid, {"title": "复现", "body": "ok"})
    bounties.resolve_bounty(store, pub_priv, bid, True, "接受")

    assert ledger.balance(store, ledger.escrow_account(bid)) == 0, "escrow 必须清零"
    assert ledger.balance(store, ledger.agent_account(rep.pubkey)) == ledger.INITIAL_GRANT + 550
    assert ledger.check_invariant(store)