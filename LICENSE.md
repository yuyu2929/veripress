# License Overview 双许可总览

**一句话：个人免费用，商业用付费。**

VeriPress 采用**双许可（Dual Licensing）**发布。使用前按你的身份选一条即可：

| 你是谁 | 适用的许可 | 费用 |
|---|---|---|
| **个人**：学习、研究、试验、爱好项目、个人论文平台 | [PolyForm Noncommercial 1.0.0](LICENSE-POLYFORM.md) | **永久免费** |
| 非营利组织：高校院系、公共研究机构、慈善、教育、政府 | [PolyForm Noncommercial 1.0.0](LICENSE-POLYFORM.md) | **免费** |
| **公司 / 个体经营者 / 任何营利实体**（内部研究院、产品内嵌、对外 SaaS） | [商业许可](COMMERCIAL-LICENSE.md) | **需购买授权** |

## 1. PolyForm Noncommercial 1.0.0 —— 个人与非营利组织免费

完整法律文本见 [LICENSE-POLYFORM.md](LICENSE-POLYFORM.md)。覆盖**全部核心能力**：

- 文档发布与 Ed25519 签名、版本链、内容寻址
- 结构化研究对象（假设 / 结果 / 基准）与 claim card
- 复式账本、赏金托管 / 认领 / 结算 / 反白嫖自动结算
- Twitter provenance、人机混合层、arXiv 引用图谱
- REST API、Web 控制台、审计链、对账接口、Prometheus 指标

## 2. 商业许可 —— 企业与营利组织付费

任何**营利性实体**（公司、个体经营者及其关联机构）出于任何商业目的使用 VeriPress
（内部 agent 研究平台、嵌入自有产品、对外提供 SaaS、外包交付），都需要一份商业许可。

商业授权条款、档位与联系方式见 [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md)。

## 与企业版功能的关系

| 层 | 内容 | 个人/非营利 | 营利组织 |
|---|---|---|---|
| 核心层 | 发布、版本链、账本、赏金、审计、对账、CLI/Web | 免费 | 需商业许可 |
| 企业层 | `sso` / `compliance_report` / `multi_org` | — | 需商业许可 + 许可证 |

企业层通过 `VERIPRESS_LICENSE` 环境变量挂载 Ed25519 签名许可证
（格式 `VP1.<payload>.<sig>`），公钥随包分发（`veripress/license_public.pem`），
私钥永不出现在仓库里。许可证被篡改或过期时**静默降级社区版**，不会锁死你的数据。

## 判断不了？看这两个问题

1. **使用 VeriPress 的行为，最终是否服务于营利？** 是 → 需要商业许可。
2. **你是法律意义上的非商业组织吗？**（PolyForm "Noncommercial Organizations" 一节
   有逐字定义）是 → 免费许可覆盖你，与资金来源无关。

---

Copyright (c) 2026 yuyu2929. All rights reserved.
