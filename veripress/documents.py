"""方向0：文档系统——内容寻址 + Git-like 版本链。

与 agxiv.org 的关键差异：那边"发布即永久、不许修改"，
我们做真版本链——论文可以发新版本，每版有签名、有父哈希、可 diff。

防篡改设计：
- 签名对象 = {content, version_no, parent_hash}，链上信息全部纳入签名
- version_hash = SHA-256(规范化签名对象)，doc_id = 第一版的 version_hash
- 改任何一字节 → 哈希变 → 签名验不过 → 版本链断裂可被 detect_chain_break 发现
"""
from __future__ import annotations

import difflib
import json
import time

from .canonical import content_hash, short
from .identity import Identity
from .storage import Storage

DOC_TYPES = (
    "paper",
    "critique",
    "replication",
    "synthesis",
    "question",
    "comment",
    "endorsement",
    "note",
    "identity",
)


class DocumentError(Exception):
    pass


def _normalize_content(content: dict) -> dict:
    """补全默认字段，保证 schema 稳定。"""
    c = dict(content)
    c.setdefault("type", "paper")
    c.setdefault("title", "")
    c.setdefault("body", "")
    c.setdefault("tags", [])
    c.setdefault("references", [])
    c.setdefault("meta", {})
    if c["type"] not in DOC_TYPES:
        raise DocumentError(f"未知文档类型: {c['type']}（可选: {', '.join(DOC_TYPES)}）")
    return c


def publish(store: Storage, private_key_raw: bytes | None, content: dict,
            pubkey: str = "", signature: str = "") -> dict:
    """发布新文档（v1）或已有文档的新版本。

    通过 content["_doc_id"] 指定已有文档即发新版本；缺省为新文档。

    两种签名方式（v0.2）：
    - private_key_raw 非空：服务端代签（本地开发用；生产应关闭）
    - private_key_raw 为 None：客户端已签好，服务端用公钥验签后入库。
      **私钥永不过网**——这是企业安全评审要的那条。
    """
    c = _normalize_content(content)
    doc_id = c.pop("_doc_id", None) or c.pop("doc_id", None)

    if doc_id is None:
        version_no, parent_hash = 1, ""
    else:
        doc = store.query_one(
            "SELECT latest_version FROM documents WHERE doc_id=?", (doc_id,)
        )
        if not doc:
            raise DocumentError(f"文档不存在: {short(doc_id)}")
        version_no = doc["latest_version"] + 1
        parent = store.query_one(
            "SELECT version_hash FROM versions WHERE doc_id=? AND version_no=?",
            (doc_id, version_no - 1),
        )
        if not parent:
            raise DocumentError("版本链断裂：找不到父版本")
        parent_hash = parent["version_hash"]

    # pubkey 从私钥推导，或由调用方提供（客户端签名模式）
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if private_key_raw is not None:
        pk = Ed25519PrivateKey.from_private_bytes(private_key_raw)
        pubkey = base64.b64encode(pk.public_key().public_bytes_raw()).decode()

    if not pubkey:
        raise DocumentError("缺少签名者身份：需要 private_key 或 pubkey+signature")

    signed_payload = {"content": c, "version_no": version_no, "parent_hash": parent_hash}

    if private_key_raw is not None:
        signature = Identity.sign(private_key_raw, signed_payload)
    else:
        if not signature:
            raise DocumentError("客户端签名模式必须提供 signature")
        if not Identity.verify(pubkey, signed_payload, signature):
            raise DocumentError("签名校验失败：内容/版本号/父哈希与签名不匹配")

    version_hash = content_hash(signed_payload)

    if doc_id is None:
        doc_id = version_hash  # doc_id = v1 的哈希，自证起源
        store.execute(
            "INSERT INTO documents(doc_id, latest_version, doc_type, title, author_pubkey, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (doc_id, 1, c["type"], c["title"], pubkey, time.time()),
        )

    store.execute(
        "INSERT INTO versions(version_hash, doc_id, version_no, content_json, parent_hash,"
        " signature, signed_by, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            version_hash,
            doc_id,
            version_no,
            json.dumps(c, ensure_ascii=False),
            parent_hash,
            signature,
            pubkey,
            time.time(),
        ),
    )
    store.execute(
        "UPDATE documents SET latest_version=?, doc_type=?, title=? WHERE doc_id=?",
        (version_no, c["type"], c["title"], doc_id),
    )
    return {"doc_id": doc_id, "version": version_no, "version_hash": version_hash}


def get_document(store: Storage, doc_id: str) -> dict | None:
    doc = store.query_one("SELECT * FROM documents WHERE doc_id=?", (doc_id,))
    if not doc:
        return None
    ver = store.query_one(
        "SELECT * FROM versions WHERE doc_id=? AND version_no=?",
        (doc_id, doc["latest_version"]),
    )
    content = json.loads(ver["content_json"])
    return {
        "doc_id": doc_id,
        "version": ver["version_no"],
        "version_hash": ver["version_hash"],
        "parent_hash": ver["parent_hash"],
        "author_pubkey": ver["signed_by"],
        "signature": ver["signature"],
        "created_at": ver["created_at"],
        "content": content,
    }


def get_version(store: Storage, doc_id: str, version_no: int) -> dict | None:
    ver = store.query_one(
        "SELECT * FROM versions WHERE doc_id=? AND version_no=?",
        (doc_id, version_no),
    )
    if not ver:
        return None
    return {
        "doc_id": doc_id,
        "version": ver["version_no"],
        "version_hash": ver["version_hash"],
        "parent_hash": ver["parent_hash"],
        "author_pubkey": ver["signed_by"],
        "signature": ver["signature"],
        "created_at": ver["created_at"],
        "content": json.loads(ver["content_json"]),
    }


def get_versions(store: Storage, doc_id: str) -> list[dict]:
    rows = store.query_all(
        "SELECT version_no, version_hash, parent_hash, signed_by, created_at"
        " FROM versions WHERE doc_id=? ORDER BY version_no",
        (doc_id,),
    )
    return [dict(r) for r in rows]


def verify_document(store: Storage, doc_id: str) -> dict:
    """验证当前版本：签名有效 + 版本链完整。"""
    doc = store.query_one("SELECT * FROM documents WHERE doc_id=?", (doc_id,))
    if not doc:
        return {"ok": False, "reason": "文档不存在"}
    problems = []
    for row in store.query_all(
        "SELECT * FROM versions WHERE doc_id=? ORDER BY version_no", (doc_id,)
    ):
        content = json.loads(row["content_json"])
        signed_payload = {
            "content": content,
            "version_no": row["version_no"],
            "parent_hash": row["parent_hash"],
        }
        if content_hash(signed_payload) != row["version_hash"]:
            problems.append(f"v{row['version_no']}: 哈希不匹配（内容被改）")
        if not Identity.verify(row["signed_by"], signed_payload, row["signature"]):
            problems.append(f"v{row['version_no']}: 签名无效")
        if row["version_no"] > 1:
            parent = store.query_one(
                "SELECT version_hash FROM versions WHERE doc_id=? AND version_no=?",
                (doc_id, row["version_no"] - 1),
            )
            if not parent or parent["version_hash"] != row["parent_hash"]:
                problems.append(f"v{row['version_no']}: 父哈希断裂")
    return {
        "ok": not problems,
        "doc_id": doc_id,
        "versions": doc["latest_version"],
        "problems": problems,
    }


def diff_versions(store: Storage, doc_id: str, v1: int, v2: int) -> dict:
    """两版 body 的 unified diff（人类可读的论文修订记录）。"""
    a = get_version(store, doc_id, v1)
    b = get_version(store, doc_id, v2)
    if not a or not b:
        raise DocumentError("版本不存在")
    diff = list(
        difflib.unified_diff(
            a["content"]["body"].splitlines(),
            b["content"]["body"].splitlines(),
            fromfile=f"v{v1}",
            tofile=f"v{v2}",
            lineterm="",
        )
    )
    return {
        "doc_id": doc_id,
        "from": v1,
        "to": v2,
        "diff": "\n".join(diff),
        "changed_lines": sum(
            1 for l in diff if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))
        ),
    }


def list_documents(
    store: Storage,
    doc_type: str | None = None,
    tag: str | None = None,
    author: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    sql = "SELECT d.*, v.created_at AS v_created FROM documents d"
    sql += " JOIN versions v ON v.doc_id=d.doc_id AND v.version_no=d.latest_version"
    conds, params = [], []
    if doc_type:
        conds.append("d.doc_type=?")
        params.append(doc_type)
    if author:
        conds.append("d.author_pubkey=?")
        params.append(author)
    if tag:
        conds.append("EXISTS (SELECT 1 FROM json_each(json_extract(v.content_json,'$.tags')) je WHERE je.value=?)")
        params.append(tag)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY v.created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    out = []
    for r in store.query_all(sql, tuple(params)):
        content = json.loads(
            store.query_one(
                "SELECT content_json FROM versions WHERE doc_id=? AND version_no=?",
                (r["doc_id"], r["latest_version"]),
            )["content_json"]
        )
        out.append(
            {
                "doc_id": r["doc_id"],
                "version": r["latest_version"],
                "type": r["doc_type"],
                "title": r["title"],
                "author_pubkey": r["author_pubkey"],
                "tags": content.get("tags", []),
                "abstract": content.get("meta", {}).get("abstract", ""),
                "created_at": r["created_at"],
            }
        )
    return out


def search(store: Storage, q: str, limit: int = 20) -> list[dict]:
    """FTS5 全文检索。"""
    rows = store.query_all(
        "SELECT doc_id, title, snippet(documents_fts, 2, '<b>', '</b>', '…', 12) AS snip"
        " FROM documents_fts WHERE documents_fts MATCH ? ORDER BY rank LIMIT ?",
        (q, limit),
    )
    return [dict(r) for r in rows]


def register_agent(store: Storage, identity: Identity) -> dict:
    """登记 agent 公开资料（身份本身不需要注册即可签名，这里只存档案）。"""
    store.execute(
        "INSERT OR REPLACE INTO agents(pubkey, display_name, bio, interests_json,"
        " provenance_twitter, created_at) VALUES(?,?,?,?,?,?)",
        (
            identity.pubkey,
            identity.display_name,
            identity.bio,
            json.dumps(identity.interests, ensure_ascii=False),
            identity.provenance_twitter,
            identity.created_at,
        ),
    )
    return identity.to_public_dict()


def get_agent(store: Storage, pubkey: str) -> dict | None:
    row = store.query_one("SELECT * FROM agents WHERE pubkey=?", (pubkey,))
    if not row:
        return None
    d = dict(row)
    d["interests"] = json.loads(d.pop("interests_json"))
    return d
