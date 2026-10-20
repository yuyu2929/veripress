"""API 冒烟测试：关键端点全链路。"""
import base64

import pytest
from fastapi.testclient import TestClient

from veripress.api import create_app
from veripress.identity import Identity
from veripress.storage import Storage


@pytest.fixture
def client():
    store = Storage(":memory:")
    return TestClient(create_app(store)), store


def _key(priv_raw: bytes) -> str:
    return base64.b64encode(priv_raw).decode()


def test_health(client):
    c, _ = client
    assert c.get("/api/v1/health").json()["status"] == "ok"


def test_full_lifecycle_via_api(client):
    c, store = client
    a, pa = Identity.generate("Pub")
    b, pb = Identity.generate("Rep")

    # 注册
    r = c.post("/api/v1/agents/register", json={"private_key": _key(pa),
                                                "display_name": "Pub"})
    assert r.status_code == 200
    assert r.json()["balance"] == 1000

    # 发论文
    r = c.post("/api/v1/documents", json={"private_key": _key(pa),
                                          "content": {"type": "paper", "title": "API论文",
                                                      "body": "body", "tags": ["api"]}})
    assert r.status_code == 200
    doc_id = r.json()["doc_id"]

    # 升级为 RO
    r = c.post("/api/v1/research-objects", json={
        "private_key": _key(pa), "doc_id": doc_id, "ro_type": "HYPOTHESIS",
        "claim": "X causes Y", "falsifiable_by": "Z absent", "confidence": 0.7})
    assert r.status_code == 200
    ro_id = r.json()["ro_id"]

    # 挂赏金
    r = c.post("/api/v1/bounties", json={"private_key": _key(pa), "ro_id": ro_id,
                                         "amount": 250, "description": "验证"})
    assert r.status_code == 200
    bounty_id = r.json()["bounty_id"]

    # 复现者认领 + 提交
    c.post("/api/v1/agents/register", json={"private_key": _key(pb), "display_name": "Rep"})
    r = c.post(f"/api/v1/bounties/{bounty_id}/claim", json={"private_key": _key(pb)})
    assert r.status_code == 200 and r.json()["status"] == "claimed"
    r = c.post(f"/api/v1/bounties/{bounty_id}/replication",
               json={"private_key": _key(pb),
                     "content": {"title": "复现OK", "body": "confirmed"}})
    assert r.status_code == 200 and r.json()["status"] == "submitted"

    # 裁决结算
    r = c.post(f"/api/v1/bounties/{bounty_id}/resolve",
               json={"private_key": _key(pa), "accept": True, "note": "good"})
    assert r.status_code == 200 and r.json()["status"] == "resolved"

    # 余额正确：复现者 1000 赠款 + 250 结算（pubkey 走查询参数，base64 安全）
    assert c.get("/api/v1/agents", params={"pubkey": b.pubkey}).json()["balance"] == 1250
    # 账本守恒
    assert c.get("/api/v1/ledger").json()["invariant_ok"] is True


def test_business_error_returns_400(client):
    c, store = client
    a, pa = Identity.generate("X")
    r = c.post("/api/v1/bounties", json={"private_key": _key(pa), "ro_id": "不存在",
                                         "amount": 10})
    assert r.status_code == 400


def test_bad_private_key_returns_400(client):
    c, _ = client
    r = c.post("/api/v1/documents", json={"private_key": "zzzz",
                                          "content": {"type": "paper", "title": "x"}})
    assert r.status_code == 400


def test_verify_signature_endpoint(client):
    c, store = client
    a, pa = Identity.generate("V")
    content = {"title": "t"}
    sig = Identity.sign(pa, content)
    r = c.post("/api/v1/documents/verify",
               json={"content": content, "pubkey": a.pubkey, "signature": sig})
    assert r.json()["ok"] is True


def test_agent_query_with_url_unsafe_pubkey(client):
    """base64 公钥含 '/' 和 '+' 时必须能查询（URL 路径传参会 404 的陷阱）。"""
    c, store = client
    # 生成一个含 URL 危险字符的公钥（约 50% 概率，循环直到命中）
    for _ in range(200):
        a, pa = Identity.generate("UrlUnsafe")
        if "/" in a.pubkey or "+" in a.pubkey:
            break
    else:
        pytest.skip("未生成到含危险字符的公钥")
    c.post("/api/v1/agents/register", json={"private_key": _key(pa)})
    r = c.get("/api/v1/agents", params={"pubkey": a.pubkey})
    assert r.status_code == 200
    assert r.json()["balance"] == 1000


def test_doc_verify_chain(client):
    c, store = client
    a, pa = Identity.generate("V")
    c.post("/api/v1/documents", json={"private_key": _key(pa),
                                      "content": {"type": "paper", "title": "v1",
                                                  "body": "1"}})
    r = c.post("/api/v1/documents", json={"private_key": _key(pa),
                                          "content": {"type": "paper", "title": "v2",
                                                      "body": "2"}})
    doc_id = r.json()["doc_id"]
    v = c.get(f"/api/v1/documents/{doc_id}/verify").json()
    assert v["ok"] and v["versions"] == 1  # 新文档
