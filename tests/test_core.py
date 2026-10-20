"""方向0 测试：身份、签名、文档、版本链、检索。"""
import json

import pytest

from veripress import documents
from veripress.canonical import canonical, content_hash
from veripress.identity import Identity
from veripress.storage import Storage


@pytest.fixture
def store():
    return Storage(":memory:")


@pytest.fixture
def agent_a(store):
    ident, priv = Identity.generate("AgentA", bio="测试agent", interests=["ml"])
    documents.register_agent(store, ident)
    return ident, priv


@pytest.fixture
def agent_b(store):
    ident, priv = Identity.generate("AgentB")
    documents.register_agent(store, ident)
    return ident, priv


def test_canonical_stable():
    a = canonical({"b": 1, "a": [1, 2]})
    b = canonical({"a": [1, 2], "b": 1})
    assert a == b
    assert content_hash({"x": 1}) == content_hash({"x": 1})


def test_identity_sign_verify(agent_a):
    ident, priv = agent_a
    content = {"title": "hi", "body": "world"}
    sig = Identity.sign(priv, content)
    assert Identity.verify(ident.pubkey, content, sig)
    # 改一个字节必须验不过
    assert not Identity.verify(ident.pubkey, {"title": "hi", "body": "world!"}, sig)
    # 换个人验不过
    other, _ = Identity.generate()
    assert not Identity.verify(other.pubkey, content, sig)


def test_publish_and_get(store, agent_a):
    ident, priv = agent_a
    r = documents.publish(store, priv, {"type": "paper", "title": "T1",
                                        "body": "B1", "tags": ["ml"]})
    doc = documents.get_document(store, r["doc_id"])
    assert doc["version"] == 1
    assert doc["content"]["title"] == "T1"
    assert doc["author_pubkey"] == ident.pubkey
    assert r["version_hash"] == r["doc_id"]  # doc_id = v1 哈希


def test_version_chain(store, agent_a):
    ident, priv = agent_a
    r1 = documents.publish(store, priv, {"type": "paper", "title": "V1", "body": "v1 body"})
    r2 = documents.publish(store, priv, {"type": "paper", "title": "V2",
                                         "body": "v2 body", "_doc_id": r1["doc_id"]})
    assert r2["version"] == 2
    doc = documents.get_document(store, r1["doc_id"])
    assert doc["version"] == 2 and doc["content"]["title"] == "V2"
    v1 = documents.get_version(store, r1["doc_id"], 1)
    assert v1["content"]["title"] == "V1"
    # 版本链验证
    ok = documents.verify_document(store, r1["doc_id"])
    assert ok["ok"] and ok["versions"] == 2


def test_version_chain_tamper_detected(store, agent_a):
    """直接篡改 versions 表内容 → 哈希/签名/父链三重校验必须抓到。"""
    ident, priv = agent_a
    r1 = documents.publish(store, priv, {"type": "paper", "title": "V1", "body": "v1"})
    documents.publish(store, priv, {"type": "paper", "title": "V2", "body": "v2",
                                    "_doc_id": r1["doc_id"]})
    # 模拟攻击者改库
    row = store.query_one(
        "SELECT content_json FROM versions WHERE doc_id=? AND version_no=1",
        (r1["doc_id"],))
    tampered = json.loads(row["content_json"])
    tampered["body"] = "被篡改的内容"
    store.execute("UPDATE versions SET content_json=? WHERE doc_id=? AND version_no=1",
                  (json.dumps(tampered), r1["doc_id"]))
    result = documents.verify_document(store, r1["doc_id"])
    assert not result["ok"]
    assert any("哈希不匹配" in p or "父哈希断裂" in p for p in result["problems"])


def test_diff_versions(store, agent_a):
    ident, priv = agent_a
    r1 = documents.publish(store, priv, {"type": "paper", "title": "D", "body": "line1\nline2"})
    documents.publish(store, priv, {"type": "paper", "title": "D", "body": "line1\nline2\nline3",
                                    "_doc_id": r1["doc_id"]})
    d = documents.diff_versions(store, r1["doc_id"], 1, 2)
    assert "+line3" in d["diff"]
    assert d["changed_lines"] >= 1


def test_search_fts(store, agent_a):
    ident, priv = agent_a
    documents.publish(store, priv, {"type": "paper", "title": " Reinforcement Learning Study",
                                    "body": "we test RL agents"})
    documents.publish(store, priv, {"type": "paper", "title": "Cooking Guide",
                                    "body": "boil water"})
    hits = documents.search(store, "RL")
    assert any(h["title"] == " Reinforcement Learning Study" for h in hits)
    hits2 = documents.search(store, "boil")
    assert len(hits2) == 1


def test_unknown_doc_type_rejected(store, agent_a):
    ident, priv = agent_a
    with pytest.raises(documents.DocumentError):
        documents.publish(store, priv, {"type": "bogus", "title": "x"})


def test_agent_profile(store, agent_a):
    ident, priv = agent_a
    a = documents.get_agent(store, ident.pubkey)
    assert a["display_name"] == "AgentA"
    assert a["interests"] == ["ml"]
