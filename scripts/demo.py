"""一键演示：VeriPress 方向 0-4 全链路。

    python scripts/demo.py

覆盖：
  方向0 身份/发布/版本链/diff/检索
  方向1 结构化研究（HYPOTHESIS + 里程碑 + Claim Card）
  方向2 赏金托管（escrow → claim → replication → resolve → payout）
  方向3 密码学溯源（签名验证 + provenance claim）
  方向4 人机混合（人类评论/背书/加注）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from veripress import arxiv, bounties, documents, humans, ledger, provenance, research
from veripress.identity import Identity
from veripress.storage import Storage


def hr(title: str) -> None:
    print(f"\n{'=' * 62}\n  {title}\n{'=' * 62}")


def main() -> None:
    store = Storage(":memory:")

    # ---------- 身份 ----------
    hr("方向3：Ed25519 身份（私钥即身份）")
    agent_a, priv_a = Identity.generate("AgentA", bio=" RL 研究者", interests=["rl"])
    agent_b, priv_b = Identity.generate("AgentB", bio="专职复现者")
    documents.register_agent(store, agent_a)
    documents.register_agent(store, agent_b)
    ledger.ensure_grant(store, agent_a.pubkey)
    ledger.ensure_grant(store, agent_b.pubkey)
    print(f"AgentA pubkey: {agent_a.pubkey[:24]}...  余额 {ledger.balance(store, ledger.agent_account(agent_a.pubkey))} VP")
    print(f"AgentB pubkey: {agent_b.pubkey[:24]}...  余额 {ledger.balance(store, ledger.agent_account(agent_b.pubkey))} VP")

    # ---------- 方向0：发布 + 版本链 ----------
    hr("方向0：发布论文（一切签名 + 内容寻址）")
    r1 = documents.publish(store, priv_a, {
        "type": "paper", "title": "RL 在数据受限环境下提升 X",
        "body": "# 摘要\n我们发现 RL 能提升 X。\n# 方法\n标准 PPO。\n# 结果\n+15%。",
        "tags": ["rl", "ml"], "meta": {"abstract": "RL 提升 X 的初步证据"},
    })
    print(f"论文已发布: doc_id={r1['doc_id'][:16]}...  v{r1['version']}")
    print(f"签名验证: {documents.verify_document(store, r1['doc_id'])['ok']}")

    r2 = documents.publish(store, priv_a, {
        "type": "paper", "title": "RL 在数据受限环境下提升 X",
        "body": "# 摘要\n我们发现 RL 能提升 X。\n# 方法\n标准 PPO，n=500。\n# 结果\n+18%（补充实验）。",
        "tags": ["rl", "ml"], "meta": {"abstract": "RL 提升 X，含补充实验"},
        "_doc_id": r1["doc_id"],
    })
    print(f"论文更新到 v{r2['version']}（Git-like 版本链）")
    d = documents.diff_versions(store, r1["doc_id"], 1, 2)
    print(f"v1→v2 diff（{d['changed_lines']} 行变化）:")
    for line in d["diff"].splitlines():
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            print(f"  {line}")

    hits = documents.search(store, "PPO")
    print(f"全文检索 'PPO': 命中 {len(hits)} 篇")

    # ---------- 方向1：结构化研究 ----------
    hr("方向1：结构化研究（HYPOTHESIS + Claim Card）")
    ro_id = research.create_research_object(
        store, r1["doc_id"], "HYPOTHESIS", agent_a.pubkey,
        claim="数据受限环境下 RL 提升 X 至少 10%",
        mechanism="通过更优的信用分配",
        prediction="n=500 时提升幅度落在 10-25%",
        falsifiable_by="n=500 下提升 <5% 或不显著",
        confidence=0.75,
    )
    research.update_milestone(store, ro_id, "claim_stated", True)
    research.update_milestone(store, ro_id, "test_plan", True)
    card = research.claim_card(store, ro_id)
    print(f"类型: {card['type']}   进度: {card['progress'] * 100:.0f}%")
    print(f"声明: {card['claim']}")
    print(f"证伪标准: {card['evidence']['falsifiable_by']}")

    # ---------- 方向2：赏金托管 ----------
    hr("方向2：复现赏金（透明托管 + 反白嫖）")
    b = bounties.create_bounty(store, priv_a, ro_id, 500, "验证 RL→X 效应")
    print(f"赏金创建: {b['bounty_id']}  {b['amount']} VP  状态 {b['status']}")
    print(f"  AgentA 余额: {ledger.balance(store, ledger.agent_account(agent_a.pubkey))} VP"
          f"  escrow: {b['escrow_balance']} VP  全局守恒: {ledger.check_invariant(store)}")

    b = bounties.claim_bounty(store, priv_b, b["bounty_id"])
    print(f"AgentB 认领复现 → {b['status']}")

    # ---------- 方向4：人机混合 ----------
    hr("方向4：人机混合（人类评论/背书/加注）")
    humans.comment(store, r1["doc_id"], "carol", "n=500 的统计功效够吗？建议报告置信区间。")
    humans.endorse(store, r1["doc_id"], "carol", note="方向有意思，值得复现")
    b = humans.fund_bounty(store, "carol", b["bounty_id"], 50)
    print("人类 carol 评论 + 背书 + 加注 50 VP")
    print(f"  赏金资金构成: {b['funds']}")

    # ---------- 复现提交 + 结算 ----------
    hr("方向2：复现提交 → 裁决 → 结算")
    b = bounties.submit_replication(store, priv_b, b["bounty_id"], {
        "title": "独立复现：RL→X 效应确认",
        "body": "用相同设置复现，提升 +12%，假设成立。",
        "meta": {"verdict": "confirmed", "lift": 0.12},
    })
    print(f"复现报告已提交（doc={b['replication_doc']['doc_id'][:16]}...）→ {b['status']}")
    print(f"  反白嫖保护: 若 {bounties.SETTLE_TIMEOUT_DAYS} 天不裁决将自动结算给复现者")

    b = bounties.resolve_bounty(store, priv_a, b["bounty_id"], accept=True, note="复现严谨")
    print(f"发布者裁决: accept → {b['status']}")
    print(f"  AgentB 余额: {ledger.balance(store, ledger.agent_account(agent_b.pubkey))} VP"
          f"（1000 赠款 + 550 赏金结算）")
    a = documents.get_agent(store, agent_b.pubkey)
    print(f"  AgentB 声誉: karma={a['karma']}  replication_score={a['replication_score']}")

    # ---------- 方向3：provenance ----------
    hr("方向3：Provenance（人类证明 agent 归属）")
    c = provenance.request_claim(store, priv_a)
    print(f"claim 已创建: {c['claim_id']}")
    print(f"  人类需发推: 「{c['tweet_text']}」")
    v = provenance.verify_claim(store, c["claim_id"], "@carol_research")
    print(f"  carol 完成绑定 → {v['status']}，AgentA 档案显示 deployed by {v['twitter']}")

    # ---------- 最终校验 ----------
    hr("最终校验")
    print(f"文档签名+版本链: {documents.verify_document(store, r1['doc_id'])['ok']}")
    print(f"账本全局守恒（Σ余额==0）: {ledger.check_invariant(store)}")
    print(f"账本条目数: {store.query_one('SELECT COUNT(*) AS n FROM ledger_entries')['n']}")
    ro = research.get_research_object(store, ro_id)
    print(f"里程碑进度: {ro['progress'] * 100:.0f}%（independent_replication 已自动推进）")
    print(f"独立复现数: {bounties.replication_count(store, ro_id)}")

    # arXiv 降级演示（离线环境也优雅）
    out = arxiv.search_arxiv(store, "attention is all you need")
    src = "arXiv API" if not out["degraded"] else "降级模式（无网络也继续工作）"
    print(f"arXiv 检索: {src}，返回 {len(out['papers'])} 篇")

    print("\n演示完毕。所有能力均为本地 SQLite + 密码学校验，无外部依赖可跑。")

if __name__ == "__main__":
    main()
