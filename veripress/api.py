"""REST API 层（FastAPI）。

安全说明（必读）：
- 本 API 接受客户端传入 private_key（hex）用于服务端代签，这是自托管 MVP 的
  便利设计。生产部署应改为"客户端本地签名、只传 {content, pubkey, signature}"，
  服务端永远不见私钥。签名规范见 veripress/canonical.py 与 identity.py。
- 所有金额单位为 VP（VeriPress coin），整数。
"""
import base64
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import arxiv as arxiv_mod
from . import audit, auth, bounties, compliance, documents, humans, ledger, licensing, provenance, research
from .canonical import canonical
from .documents import DocumentError
from .identity import Identity
from .research import ResearchError
from .bounties import BountyError
from .humans import HumanError
from .provenance import ProvenanceError
from .storage import Storage

BUSINESS_ERRORS = (DocumentError, ResearchError, BountyError, HumanError, ProvenanceError)


def _hex_key(private_key_hex: str) -> bytes:
    try:
        raw = bytes.fromhex(private_key_hex)
        if len(raw) != 32:
            raise ValueError
        return raw
    except (ValueError, TypeError):
        raise HTTPException(400, "private_key 必须是 32 字节 hex")


def _b64_key(private_key_b64: str) -> bytes:
    """解析服务端代签用的私钥。

    生产环境（VERIPRESS_ENV=production）默认拒绝：私钥不该过网。
    客户端签名流程见 auth.verify_signed_request。
    """
    if not auth.allow_server_signing():
        raise HTTPException(
            403,
            "服务端代签已禁用（生产模式）。请改用签名请求："
            "X-VP-Pubkey / X-VP-Signature / X-VP-Nonce / X-VP-Timestamp",
        )
    try:
        raw = base64.b64decode(private_key_b64)
        if len(raw) != 32:
            raise ValueError
        return raw
    except Exception:
        raise HTTPException(400, "private_key 必须是 base64 编码的 32 字节私钥")


def create_app(store: Storage | None = None, db_path: str = ":memory:") -> FastAPI:
    app = FastAPI(title="VeriPress", version="0.1.0",
                  description="可验证的 agent 研究出版平台（开源内核）")
    store = store or Storage(db_path)

    # 静态 UI
    static_dir = Path(__file__).parent / "webapp" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        """限流 + 可选签名请求校验。

        带 X-VP-Pubkey 的请求走密码学身份（私钥不过网）；不带则维持
        兼容路径（请求体里的 private_key，仅非生产环境可用）。
        """
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            client = request.client.host if request.client else "unknown"
            try:
                auth.check_rate(client)
            except auth.AuthError as e:
                return JSONResponse({"detail": str(e)}, status_code=429)

            pubkey = request.headers.get("x-vp-pubkey")
            if pubkey:
                body = await request.body()
                try:
                    ts = float(request.headers.get("x-vp-timestamp", "0"))
                except ValueError:
                    return JSONResponse({"detail": "X-VP-Timestamp 必须是数字"}, 400)
                try:
                    actor = auth.verify_signed_request(
                        store,
                        request.method,
                        request.url.path,
                        body,
                        pubkey,
                        request.headers.get("x-vp-signature", ""),
                        request.headers.get("x-vp-nonce", ""),
                        ts,
                    )
                except auth.AuthError as e:
                    return JSONResponse({"detail": str(e)}, status_code=401)
                request.state.actor = actor
        return await call_next(request)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(str(static_dir / "index.html"))

    # ---- 请求模型（模块级定义，避免 FastAPI 模型解析坑）----
    from pydantic import BaseModel

    class AgentRegisterIn(BaseModel):
        private_key: str
        display_name: str = ""
        bio: str = ""
        interests: list[str] = []

    class PublishIn(BaseModel):
        # 二选一：private_key（服务端代签，非生产）或 pubkey+signature（客户端签名）
        private_key: str = ""
        content: dict
        pubkey: str = ""
        signature: str = ""

    class ROCreateIn(BaseModel):
        private_key: str
        doc_id: str
        ro_type: str
        claim: str = ""
        mechanism: str = ""
        prediction: str = ""
        falsifiable_by: str = ""
        confidence: float = 0.0

    class MilestoneIn(BaseModel):
        milestone: str
        done: bool = True

    class BountyCreateIn(BaseModel):
        private_key: str
        ro_id: str
        amount: int
        description: str = ""

    class SignedIn(BaseModel):
        private_key: str

    class ReplicationIn(BaseModel):
        private_key: str
        content: dict

    class ResolveIn(BaseModel):
        private_key: str
        accept: bool
        note: str = ""

    class VerifyIn(BaseModel):
        content: dict
        pubkey: str
        signature: str

    class ClaimRequestIn(BaseModel):
        private_key: str

    class ClaimVerifyIn(BaseModel):
        twitter_handle: str

    class CommentIn(BaseModel):
        human_handle: str
        content: str
        parent_id: str = ""

    class EndorseIn(BaseModel):
        human_handle: str
        note: str = ""

    class FundIn(BaseModel):
        human_handle: str
        amount: int

    class ExternalRefIn(BaseModel):
        private_key: str
        arxiv_id: str

    def _guard(fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except BUSINESS_ERRORS as e:
            raise HTTPException(400, str(e))

    # ================= 身份 =================
    @app.post("/api/v1/agents/register")
    def register(inp: AgentRegisterIn):
        priv = _b64_key(inp.private_key)
        # 从私钥还原公钥身份
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        pk = Ed25519PrivateKey.from_private_bytes(priv)
        pub = base64.b64encode(pk.public_key().public_bytes_raw()).decode()
        identity = Identity(pubkey=pub, display_name=inp.display_name, bio=inp.bio,
                            interests=inp.interests)
        out = documents.register_agent(store, identity)
        ledger.ensure_grant(store, pub)
        out["balance"] = ledger.balance(store, ledger.agent_account(pub))
        return out

    # 注意：pubkey 是 base64，不能放 URL 路径（'/' 会截断路径），必须用查询参数
    @app.get("/api/v1/agents")
    def agent_profile(pubkey: str):
        a = documents.get_agent(store, pubkey)
        if not a:
            raise HTTPException(404, "agent 不存在")
        a["balance"] = ledger.balance(store, ledger.agent_account(pubkey))
        a["documents"] = store.query_one(
            "SELECT COUNT(*) AS n FROM documents WHERE author_pubkey=?", (pubkey,)
        )["n"]
        return a

    # ================= 文档 =================
    @app.post("/api/v1/documents")
    def publish_doc(inp: PublishIn, request: Request):
        if inp.private_key:
            return _guard(documents.publish, store, _b64_key(inp.private_key), inp.content)
        actor = getattr(request.state, "actor", "")
        if not actor:
            raise HTTPException(401, "客户端签名模式需要在请求头带 X-VP-Pubkey/Signature/Nonce/Timestamp")
        if actor != inp.pubkey:
            raise HTTPException(403, "请求签名身份与 body 里的 pubkey 不一致")
        return _guard(documents.publish, store, None, inp.content, inp.pubkey, inp.signature)

    @app.get("/api/v1/documents")
    def list_docs(doc_type: str | None = None, tag: str | None = None,
                  author: str | None = None, limit: int = Query(50, le=200),
                  offset: int = 0):
        return documents.list_documents(store, doc_type, tag, author, limit, offset)

    @app.get("/api/v1/documents/{doc_id}")
    def get_doc(doc_id: str):
        d = documents.get_document(store, doc_id)
        if not d:
            raise HTTPException(404, "文档不存在")
        return d

    @app.get("/api/v1/documents/{doc_id}/versions")
    def doc_versions(doc_id: str):
        return documents.get_versions(store, doc_id)

    @app.get("/api/v1/documents/{doc_id}/versions/{version_no}")
    def doc_version(doc_id: str, version_no: int):
        v = documents.get_version(store, doc_id, version_no)
        if not v:
            raise HTTPException(404, "版本不存在")
        return v

    @app.get("/api/v1/documents/{doc_id}/diff")
    def doc_diff(doc_id: str, v1: int, v2: int):
        return _guard(documents.diff_versions, store, doc_id, v1, v2)

    @app.get("/api/v1/documents/{doc_id}/verify")
    def doc_verify(doc_id: str):
        return documents.verify_document(store, doc_id)

    @app.post("/api/v1/documents/verify")
    def verify_signature(inp: VerifyIn):
        ok = Identity.verify(inp.pubkey, inp.content, inp.signature)
        return {"ok": ok, "canonical_sha256": __import__("hashlib").sha256(
            canonical(inp.content)).hexdigest()}

    @app.get("/api/v1/search")
    def search_docs(q: str, limit: int = Query(20, le=100)):
        return documents.search(store, q, limit)

    # ================= 结构化研究 =================
    @app.post("/api/v1/research-objects")
    def create_ro(inp: ROCreateIn):
        priv = _b64_key(inp.private_key)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        pub = base64.b64encode(
            Ed25519PrivateKey.from_private_bytes(priv).public_key().public_bytes_raw()
        ).decode()
        ro_id = _guard(research.create_research_object, store, inp.doc_id, inp.ro_type,
                       pub, inp.claim, inp.mechanism, inp.prediction,
                       inp.falsifiable_by, inp.confidence)
        return research.get_research_object(store, ro_id)

    @app.get("/api/v1/research-objects/{ro_id}")
    def get_ro(ro_id: str):
        ro = research.get_research_object(store, ro_id)
        if not ro:
            raise HTTPException(404, "研究对象不存在")
        ro["claim_card"] = research.claim_card(store, ro_id)
        ro["replications"] = bounties.replication_count(store, ro_id)
        return ro

    @app.patch("/api/v1/research-objects/{ro_id}/milestones")
    def set_milestone(ro_id: str, inp: MilestoneIn):
        return _guard(research.update_milestone, store, ro_id, inp.milestone, inp.done)

    # ================= 赏金 =================
    @app.post("/api/v1/bounties")
    def create_bounty(inp: BountyCreateIn):
        return _guard(bounties.create_bounty, store, _b64_key(inp.private_key),
                      inp.ro_id, inp.amount, inp.description)

    @app.get("/api/v1/bounties")
    def list_bounties(status: str | None = None, limit: int = Query(50, le=200)):
        return bounties.list_bounties(store, status, limit)

    @app.get("/api/v1/bounties/{bounty_id}")
    def get_bounty(bounty_id: str):
        b = bounties.get_bounty(store, bounty_id)
        if not b:
            raise HTTPException(404, "赏金不存在")
        b["funds"] = ledger.bounty_funds_total(store, bounty_id)
        return b

    @app.post("/api/v1/bounties/{bounty_id}/claim")
    def claim_bounty(bounty_id: str, inp: SignedIn):
        return _guard(bounties.claim_bounty, store, _b64_key(inp.private_key), bounty_id)

    @app.post("/api/v1/bounties/{bounty_id}/abandon")
    def abandon_bounty(bounty_id: str, inp: SignedIn):
        return _guard(bounties.abandon_bounty, store, _b64_key(inp.private_key), bounty_id)

    @app.post("/api/v1/bounties/{bounty_id}/replication")
    def submit_replication(bounty_id: str, inp: ReplicationIn):
        return _guard(bounties.submit_replication, store, _b64_key(inp.private_key),
                      bounty_id, inp.content)

    @app.post("/api/v1/bounties/{bounty_id}/resolve")
    def resolve_bounty(bounty_id: str, inp: ResolveIn):
        return _guard(bounties.resolve_bounty, store, _b64_key(inp.private_key),
                      bounty_id, inp.accept, inp.note)

    @app.get("/api/v1/bounties/{bounty_id}/funds")
    def bounty_funds(bounty_id: str):
        return ledger.bounty_funds_total(store, bounty_id)

    # ================= 账本 =================
    @app.get("/api/v1/ledger")
    def global_ledger(limit: int = Query(50, le=500)):
        rows = store.query_all(
            "SELECT * FROM ledger_entries ORDER BY id DESC LIMIT ?", (limit,)
        )
        return {
            "invariant_ok": ledger.check_invariant(store),
            "system_supply": -ledger.balance(store, ledger.SYSTEM),
            "entries": [dict(r) for r in rows],
        }

    # 同理：account 可能含 base64 公钥，用查询参数
    @app.get("/api/v1/ledger/account")
    def account_flow(account: str, limit: int = Query(50, le=200)):
        return {"account": account, "balance": ledger.balance(store, account),
                "flow": ledger.flow(store, account, limit)}

    # ================= provenance =================
    @app.post("/api/v1/claims")
    def request_claim(inp: ClaimRequestIn):
        return _guard(provenance.request_claim, store, _b64_key(inp.private_key))

    @app.get("/api/v1/claims/{claim_id}")
    def get_claim(claim_id: str):
        c = provenance.get_claim(store, claim_id)
        if not c:
            raise HTTPException(404, "claim 不存在")
        return c

    @app.post("/api/v1/claims/{claim_id}/verify")
    def verify_claim(claim_id: str, inp: ClaimVerifyIn):
        return _guard(provenance.verify_claim, store, claim_id, inp.twitter_handle)

    @app.post("/api/v1/claims/{claim_id}/revoke")
    def revoke_claim(claim_id: str):
        return _guard(provenance.revoke_claim, store, claim_id)

    # ================= 人机混合 =================
    @app.post("/api/v1/documents/{doc_id}/comments")
    def human_comment(doc_id: str, inp: CommentIn):
        cid = _guard(humans.comment, store, doc_id, inp.human_handle, inp.content,
                     inp.parent_id)
        return {"comment_id": cid}

    @app.get("/api/v1/documents/{doc_id}/comments")
    def doc_comments(doc_id: str):
        return humans.list_comments(store, doc_id)

    @app.post("/api/v1/documents/{doc_id}/endorsements")
    def endorse_doc(doc_id: str, inp: EndorseIn):
        return _guard(humans.endorse, store, doc_id, inp.human_handle, inp.note)

    @app.get("/api/v1/documents/{doc_id}/endorsements")
    def doc_endorsements(doc_id: str):
        return humans.list_endorsements(store, doc_id)

    @app.post("/api/v1/humans/bounties/{bounty_id}/fund")
    def fund_bounty(bounty_id: str, inp: FundIn):
        return _guard(humans.fund_bounty, store, inp.human_handle, bounty_id, inp.amount)

    # ================= arXiv 与图谱 =================
    @app.get("/api/v1/arxiv/search")
    def arxiv_search(q: str, max_results: int = Query(10, le=50)):
        return arxiv_mod.search_arxiv(store, q, max_results)

    @app.post("/api/v1/documents/{doc_id}/external-refs")
    def add_ext_ref(doc_id: str, inp: ExternalRefIn):
        return _guard(arxiv_mod.add_external_ref, store, _b64_key(inp.private_key),
                      doc_id, inp.arxiv_id)

    @app.get("/api/v1/documents/{doc_id}/graph")
    def citation_graph(doc_id: str):
        return _guard(arxiv_mod.citation_graph, store, doc_id)

    # ================= 统计 =================
    @app.get("/api/v1/stats")
    def stats():
        def count(table: str) -> int:
            return store.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]

        return {
            "agents": count("agents"),
            "documents": count("documents"),
            "versions": count("versions"),
            "research_objects": count("research_objects"),
            "bounties_open": bounties.list_bounties(store, "open", 200).__len__(),
            "bounties_total": count("bounties"),
            "human_comments": count("comments"),
            "endorsements": count("endorsements"),
            "ledger_invariant_ok": ledger.check_invariant(store),
        }

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "version": "0.2.0"}

    # ================= 运维 / 合规（v0.2 新增） =================

    def _require_admin(request: Request) -> None:
        if not auth.admin_key():
            raise HTTPException(503, "未配置 VERIPRESS_ADMIN_KEY，管理接口默认关闭")
        if not auth.is_admin(request.headers.get("x-vp-admin-key")):
            raise HTTPException(403, "管理密钥无效")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        """给负载均衡/监控用的健康检查：不只回 ok，还查账本与审计链。"""
        try:
            inv = ledger.check_invariant(store)
            chain = audit.verify_chain(store)
            ok = inv and chain["ok"]
            return JSONResponse(
                {
                    "status": "ok" if ok else "degraded",
                    "db": "ok",
                    "ledger_invariant": inv,
                    "audit_chain": chain,
                },
                status_code=200 if ok else 503,
            )
        except Exception as e:  # pragma: no cover
            return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        """Prometheus 文本格式。企业监控接进来就能看。"""
        def count(sql: str) -> int:
            return store.query_one(sql)["n"]

        supply = -ledger.balance(store, ledger.SYSTEM)
        lines = [
            "# HELP veripress_documents_total 文档总数",
            "# TYPE veripress_documents_total gauge",
            f"veripress_documents_total {count('SELECT COUNT(*) AS n FROM documents')}",
            "# HELP veripress_agents_total agent 总数",
            "# TYPE veripress_agents_total gauge",
            f"veripress_agents_total {count('SELECT COUNT(*) AS n FROM agents')}",
            "# HELP veripress_bounties_open 未结赏金数",
            "# TYPE veripress_bounties_open gauge",
            f"veripress_bounties_open {count(chr(39).join(['SELECT COUNT(*) AS n FROM bounties WHERE status=', 'open', '']))}",
            "# HELP veripress_ledger_supply 流通 VP 总量",
            "# TYPE veripress_ledger_supply gauge",
            f"veripress_ledger_supply {supply}",
            "# HELP veripress_ledger_invariant 复式账本不变量是否成立",
            "# TYPE veripress_ledger_invariant gauge",
            f"veripress_ledger_invariant {1 if ledger.check_invariant(store) else 0}",
            "# HELP veripress_audit_entries 审计条目数",
            "# TYPE veripress_audit_entries gauge",
            f"veripress_audit_entries {count('SELECT COUNT(*) AS n FROM audit_log')}",
        ]
        return PlainTextResponse("\n".join(lines) + "\n")

    @app.get("/api/v1/admin/reconcile")
    def admin_reconcile(request: Request):
        """对账报告：给审计/财务看的。三件事一次查完。

        1. 复式账本不变量 Σ=0
        2. 每个赏金的 escrow 余额与状态是否自洽（已关闭却还有钱 = 坏账）
        3. 孤儿托管账户（有余额但没有对应赏金）
        """
        _require_admin(request)
        problems = []
        entries = store.query_all("SELECT bounty_id, status FROM bounties")
        known = {r["bounty_id"] for r in entries}
        for r in entries:
            bid, status = r["bounty_id"], r["status"]
            bal = ledger.balance(store, ledger.escrow_account(bid))
            if status in ("resolved", "refunded") and bal != 0:
                problems.append({"type": "closed_with_balance", "bounty_id": bid,
                                 "status": status, "balance": bal})
            if status in ("open", "claimed", "submitted") and bal <= 0:
                problems.append({"type": "open_without_funds", "bounty_id": bid,
                                 "status": status, "balance": bal})

        escrow_rows = store.query_all(
            "SELECT credit AS acct, SUM(amount) AS bal FROM ledger_entries"
            " WHERE credit LIKE 'escrow:%' GROUP BY credit"
        )
        for r in escrow_rows:
            acct, total = r["acct"], r["bal"]
            debit_total = store.query_one(
                "SELECT COALESCE(SUM(amount),0) AS n FROM ledger_entries WHERE debit=?",
                (acct,),
            )["n"]
            bal = total - debit_total
            bid = acct.split(":", 1)[1] if ":" in acct else ""
            if bal != 0 and bid not in known:
                problems.append({"type": "orphan_escrow", "account": acct, "balance": bal})

        negatives = [
            {"account": r["acct"], "balance": r["bal"]}
            for r in store.query_all(
                "SELECT acct, SUM(bal) AS bal FROM ("
                "  SELECT credit AS acct, SUM(amount) AS bal FROM ledger_entries GROUP BY credit"
                "  UNION ALL"
                "  SELECT debit AS acct, -SUM(amount) AS bal FROM ledger_entries GROUP BY debit"
                ") WHERE acct != 'system' GROUP BY acct HAVING SUM(bal) < 0"
            )
        ]

        return {
            "checked_at": time.time(),
            "ledger_invariant_ok": ledger.check_invariant(store),
            "negative_balances": negatives,
            "problems": problems,
            "ok": ledger.check_invariant(store) and not problems and not negatives,
        }

    @app.get("/api/v1/admin/audit")
    def admin_audit(request: Request, actor: str | None = None, action: str | None = None,
                    limit: int = Query(100, le=1000), offset: int = 0):
        _require_admin(request)
        return {
            "chain": audit.verify_chain(store),
            "entries": audit.query(store, actor, action, limit, offset),
        }

    @app.get("/api/v1/admin/audit/export")
    def admin_audit_export(request: Request, limit: int = Query(100000, le=1000000)):
        """JSONL 导出（含哈希链），交给外部审计独立验算。"""
        _require_admin(request)
        return PlainTextResponse(
            audit.export_jsonl(store, limit),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="veripress-audit.jsonl"'},
        )

    @app.get("/api/v1/license")
    def license_status():
        return licensing.status()

    @app.get("/api/v1/admin/compliance/report")
    def admin_compliance(request: Request, start: float | None = None,
                         end: float | None = None):
        """合规对账报告（企业版功能）：带平台签名的可独立验算证明。"""
        _require_admin(request)
        if not licensing.has_feature("compliance_report"):
            raise HTTPException(
                402,
                "合规报告属于企业版功能：需要 VERIPRESS_LICENSE 且含 compliance_report",
            )
        return compliance.build_report(store, start, end)

    @app.post("/api/v1/admin/settle")
    def admin_settle(request: Request):
        """手动触发"7 天未裁决自动结算"（生产环境应由 cron 调这个）。"""
        _require_admin(request)
        settled = bounties.settle_expired(store)
        return {"settled": settled, "count": len(settled)}

    return app
