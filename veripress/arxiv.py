"""方向4：arXiv 客户端 + 引用图谱。

arXiv API（export.arxiv.org）只读、无需 key。
设计：
- 结果进本地缓存表（arxiv_cache），7 天 TTL——agent 高频检索不会打爆 arXiv
- 网络失败优雅降级：返回缓存或空列表，绝不抛错中断出版流程
- 外部引用挂在文档 meta.external_refs = ["arxiv:2401.12345", ...]
- 引用图谱 = 内部引用（references by hash）+ 外部引用（arXiv）
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .storage import Storage

ARXIV_API = "http://export.arxiv.org/api/query"
CACHE_TTL_DAYS = 7
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
TIMEOUT = 15


def _parse_entry(entry: ET.Element) -> dict:
    arxiv_id_url = entry.findtext(f"{ATOM}id", "")
    arxiv_id = arxiv_id_url.rsplit("/", 1)[-1] if arxiv_id_url else ""
    authors = [a.findtext(f"{ATOM}name", "") for a in entry.findall(f"{ATOM}author")]
    cats = [c.get("term", "") for c in entry.findall(f"{ARXIV_NS}primary_category")]
    cats += [c.get("term", "") for c in entry.findall(f"{ATOM}category")]
    return {
        "arxiv_id": arxiv_id,
        "title": re.sub(r"\s+", " ", entry.findtext(f"{ATOM}title", "")).strip(),
        "abstract": re.sub(r"\s+", " ", entry.findtext(f"{ATOM}summary", "")).strip(),
        "published": entry.findtext(f"{ATOM}published", ""),
        "authors": authors,
        "categories": sorted(set(cats)),
        "url": arxiv_id_url,
    }


def _cache_put(store: Storage, papers: list[dict]) -> None:
    for p in papers:
        if not p["arxiv_id"]:
            continue
        store.execute(
            "INSERT OR REPLACE INTO arxiv_cache(arxiv_id, title, authors_json, abstract,"
            " published, categories_json, fetched_at) VALUES(?,?,?,?,?,?,?)",
            (
                p["arxiv_id"],
                p["title"],
                json.dumps(p["authors"], ensure_ascii=False),
                p["abstract"],
                p["published"],
                json.dumps(p["categories"], ensure_ascii=False),
                time.time(),
            ),
        )


def search_arxiv(store: Storage, query: str, max_results: int = 10) -> dict:
    """检索 arXiv。优先缓存；miss 的查 API 并回填缓存。任何网络错误都降级。"""
    wanted = _extract_ids(query)
    cached = []
    if wanted:
        for aid in wanted:
            row = store.query_one("SELECT * FROM arxiv_cache WHERE arxiv_id=?", (aid,))
            if row and (time.time() - row["fetched_at"]) < CACHE_TTL_DAYS * 86400:
                cached.append(_row_to_paper(row))
        if len(cached) == len(wanted):
            return {"papers": cached, "source": "cache", "degraded": False}
    try:
        params = urllib.parse.urlencode(
            {"search_query": query, "max_results": max_results, "sortBy": "relevance"}
        )
        with urllib.request.urlopen(f"{ARXIV_API}?{params}", timeout=TIMEOUT) as resp:
            root = ET.fromstring(resp.read())
        papers = [_parse_entry(e) for e in root.findall(f"{ATOM}entry")]
        _cache_put(store, papers)
        return {"papers": papers, "source": "arxiv", "degraded": False}
    except Exception as e:  # 网络错误不中断出版
        return {
            "papers": cached,
            "source": "cache",
            "degraded": True,
            "warning": f"arXiv API 不可用（{type(e).__name__}），仅返回缓存结果",
        }


def _extract_ids(query: str) -> list[str]:
    return re.findall(r"(?:arxiv:)?(\d{4}\.\d{4,5})", query, re.I)


def _row_to_paper(row) -> dict:
    return {
        "arxiv_id": row["arxiv_id"],
        "title": row["title"],
        "abstract": row["abstract"],
        "published": row["published"],
        "authors": json.loads(row["authors_json"]),
        "categories": json.loads(row["categories_json"]),
        "url": f"https://arxiv.org/abs/{row['arxiv_id']}",
    }


def get_paper(store: Storage, arxiv_id: str) -> dict | None:
    row = store.query_one("SELECT * FROM arxiv_cache WHERE arxiv_id=?", (arxiv_id,))
    return _row_to_paper(row) if row else None


def add_external_ref(
    store: Storage, private_key_raw: bytes, doc_id: str, arxiv_id: str
) -> dict:
    """给文档挂一个外部 arXiv 引用——以新版本发布，签名链不断。

    注意：绝不能直接 UPDATE versions 表——那会让内容与签名不匹配，
    破坏"一切皆签名"原则。引用变更也是版本变更。
    """
    from .documents import get_document, publish

    doc = get_document(store, doc_id)
    if not doc:
        raise ValueError("文档不存在")
    content = dict(doc["content"])
    meta = dict(content.get("meta", {}))
    refs = list(meta.get("external_refs", []))
    tag = f"arxiv:{arxiv_id}"
    if tag in refs:
        return {"doc_id": doc_id, "version": doc["version"], "changed": False}
    refs.append(tag)
    meta["external_refs"] = refs
    content["meta"] = meta
    content["_doc_id"] = doc_id
    result = publish(store, private_key_raw, content)
    result["changed"] = True
    return result


def citation_graph(store: Storage, doc_id: str) -> dict:
    """引用图谱数据：内部双向引用 + 外部 arXiv 引用。"""
    from .documents import get_document

    doc = get_document(store, doc_id)
    if not doc:
        raise ValueError("文档不存在")
    content = doc["content"]
    out_refs = [r for r in content.get("references", []) if r]
    ext_refs = content.get("meta", {}).get("external_refs", [])

    # 被引：所有 references 包含 doc_id 的文档
    in_rows = store.query_all(
        "SELECT DISTINCT doc_id FROM versions"
        " WHERE EXISTS (SELECT 1 FROM json_each(json_extract(content_json,'$.references')) je"
        "               WHERE je.value=?)",
        (doc_id,),
    )
    in_refs = [r["doc_id"] for r in in_rows if r["doc_id"] != doc_id]

    # 外部引用的元数据（能拿到的）
    ext_papers = []
    for tag in ext_refs:
        aid = tag.split(":", 1)[-1]
        p = get_paper(store, aid)
        if p:
            ext_papers.append(p)

    return {
        "doc_id": doc_id,
        "cites": out_refs,
        "cited_by": in_refs,
        "external_refs": ext_refs,
        "external_papers": ext_papers,
    }
