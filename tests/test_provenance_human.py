"""方向3 + 方向4 测试：provenance、人机混合、arXiv 降级。"""
import time

import pytest

from veripress import arxiv as arxiv_mod
from veripress import documents, humans, provenance
from veripress.identity import Identity
from veripress.provenance import ProvenanceError
from veripress.storage import Storage


@pytest.fixture
def store():
    return Storage(":memory:")


@pytest.fixture
def agent(store):
    ident, priv = Identity.generate("ProvenanceAgent")
    documents.register_agent(store, ident)
    return ident, priv


@pytest.fixture
def paper(store, agent):
    ident, priv = agent
    r = documents.publish(store, priv, {"type": "paper", "title": "可引用的论文",
                                        "body": "内容", "tags": ["test"]})
    return r


# ---------- 方向3：provenance ----------

def test_provenance_full_flow(store, agent):
    ident, priv = agent
    c = provenance.request_claim(store, priv)
    assert c["claim_id"] and "tweet_text" in c
    # 未验证前 agent 没有 twitter 绑定
    assert documents.get_agent(store, ident.pubkey)["provenance_twitter"] == ""
    v = provenance.verify_claim(store, c["claim_id"], "@alice_dev")
    assert v["status"] == "verified" and v["twitter"] == "alice_dev"
    assert documents.get_agent(store, ident.pubkey)["provenance_twitter"] == "alice_dev"
    # 撤销
    r = provenance.revoke_claim(store, c["claim_id"])
    assert r["status"] == "revoked"
    assert documents.get_agent(store, ident.pubkey)["provenance_twitter"] == ""


def test_provenance_expiry(store, agent):
    ident, priv = agent
    c = provenance.request_claim(store, priv)
    store.execute("UPDATE claims SET expires_at=? WHERE claim_id=?",
                  (time.time() - 1, c["claim_id"]))
    with pytest.raises(ProvenanceError):
        provenance.verify_claim(store, c["claim_id"], "bob")


def test_provenance_one_pending_per_agent(store, agent):
    ident, priv = agent
    c1 = provenance.request_claim(store, priv)
    c2 = provenance.request_claim(store, priv)
    assert c1["claim_id"] != c2["claim_id"]
    # 旧的被清理
    assert provenance.get_claim(store, c1["claim_id"]) is None


# ---------- 方向4：人机混合 ----------

def test_human_comment_and_endorse(store, paper):
    doc_id = paper["doc_id"]
    humans.comment(store, doc_id, "alice", "这个实验设计有漏洞")
    comments = humans.list_comments(store, doc_id)
    assert len(comments) == 1 and comments[0]["author_type"] == "human"
    e = humans.endorse(store, doc_id, "bob", note="值得深挖")
    assert e["endorsements"] == 1
    # 同一个人重复背书不叠加
    humans.endorse(store, doc_id, "bob")
    assert len(humans.list_endorsements(store, doc_id)) == 1


def test_empty_comment_rejected(store, paper):
    with pytest.raises(humans.HumanError):
        humans.comment(store, paper["doc_id"], "alice", "   ")


def test_human_fund_bounty_goes_to_escrow(store, agent):
    """人类加注：钱从人类账户进 escrow，账本守恒。"""
    from veripress import bounties, ledger, research

    ident, priv = agent
    r = documents.publish(store, priv, {"type": "paper", "title": "P", "body": "b"})
    ro_id = research.create_research_object(store, r["doc_id"], "HYPOTHESIS", ident.pubkey,
                                            claim="X", falsifiable_by="Y")
    b = bounties.create_bounty(store, priv, ro_id, 200)
    out = humans.fund_bounty(store, "carol", b["bounty_id"], 50)
    assert out["funds"]["bounty"] == 200
    assert out["funds"]["human_funding"] == 50
    assert out["funds"]["total"] == 250
    assert ledger.balance(store, ledger.escrow_account(b["bounty_id"])) == 250
    assert ledger.check_invariant(store)
    assert ledger.balance(store, ledger.human_account("carol")) == ledger.INITIAL_GRANT - 50


def test_human_fund_closed_bounty_rejected(store, agent):
    from veripress import bounties, research

    ident, priv = agent
    r = documents.publish(store, priv, {"type": "paper", "title": "P", "body": "b"})
    ro_id = research.create_research_object(store, r["doc_id"], "RESULT", ident.pubkey,
                                            claim="X")
    b = bounties.create_bounty(store, priv, ro_id, 100)
    store.execute("UPDATE bounties SET status='resolved' WHERE bounty_id=?",
                  (b["bounty_id"],))
    with pytest.raises(humans.HumanError):
        humans.fund_bounty(store, "carol", b["bounty_id"], 10)


# ---------- arXiv 降级 ----------

def test_arxiv_search_degrades_gracefully(store, monkeypatch):
    """网络错误时不得抛异常，只返回降级标记。"""

    def boom(*a, **kw):
        raise OSError("no network")

    monkeypatch.setattr(arxiv_mod.urllib.request, "urlopen", boom)
    out = arxiv_mod.search_arxiv(store, "attention is all you need")
    assert out["degraded"] is True
    assert out["papers"] == []
    assert "warning" in out


def test_citation_graph(store, agent):
    ident, priv = agent
    r1 = documents.publish(store, priv, {"type": "paper", "title": "A", "body": "a"})
    r2 = documents.publish(store, priv, {"type": "paper", "title": "B", "body": "b",
                                         "references": [r1["doc_id"]]})
    g = arxiv_mod.citation_graph(store, r1["doc_id"])
    assert r2["doc_id"] in g["cited_by"]
    g2 = arxiv_mod.citation_graph(store, r2["doc_id"])
    assert r1["doc_id"] in g2["cites"]
