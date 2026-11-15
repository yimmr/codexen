# Codexen RFC 架构演进治理流程规范 📜
### Codexen Architecture RFC Governance & Evolution Process

Codexen 采用标准化 **RFC（Request for Comments，征求意见稿）** 机制管理重大的架构演进、工具协议变更及底层安全设计。本文档确立了 RFC 的编写范围、生命周期流转与评审准则。

---

## 1. 适用场景 (When to Write an RFC)

并非所有修改都需要发起 RFC。RFC 专门针对以下具有全局影响的技术决策：

| 需要发起 RFC 的场景 | 无需发起 RFC 的场景（日常 PR / Issue 即可） |
| :--- | :--- |
| 涉及 MCP 工具输入/输出 Schema、状态码或协议契约的重大变动 | 日常 Bug 修复、异常分支优化、代码小范围重构 |
| 影响核心安全边界、凭据黑名单或 HITL 授权流程的改动 | 单个工具参数的文档说明或示例更新 |
| 底座通信层、异步非阻塞架构或并发模型的重大演进 | 自动化单元测试用例扩充 |
| 引入新的全局忽略规则匹配算法或跨平台核心依赖 | 本地启动脚本（如 `start.bat`）微调 |

---

## 2. RFC 生命周期流转 (Lifecycle Stages)

每一个 RFC 从立项到归档，经历以下五个标准化状态阶段：

```mermaid
flowchart LR
    Draft["1. 草案 (Draft)<br/>提出技术设想与动机"]
    Review["2. 评审 (In Review)<br/>安全性与向下兼容性论证"]
    Accepted["3. 采纳 (Accepted)<br/>冻结数据契约与接口设计"]
    Implemented["4. 交付 (Implemented)<br/>完成代码与全量单测验收"]
    Living["5. 现行 (Living)<br/>作为基准规格持续维护"]
    Superseded["已废弃 (Superseded)<br/>由更新的 RFC 替代"]

    Draft --> Review
    Review --> Accepted
    Review -- 未达成共识 --> Draft
    Accepted --> Implemented
    Implemented --> Living
    Living --> Superseded
```

### 阶段定义说明：
1. **草案 (Draft)**：
   - 提案人基于标准模板 `docs/rfcs/template.md` 编写，在 `docs/rfcs/` 下按流水号创建（如 `0002-xxx.md`）；
   - 重点阐明背景痛点、真实事故复盘（Postmortem）与预期交付价值。
2. **评审 (In Review)**：
   - 发起技术评审，核心开发者与社区共同论证技术可行性、边界防御有效性以及跨客户端兼容性。
3. **已采纳 (Accepted)**：
   - 核心设计方案达成共识，接口签名、数据返回结构与安全边界正式**冻结**；
   - 纳入架构演进路线图（`docs/evolution-roadmap.md`）作为正式开发依据。
4. **已交付 (Implemented)**：
   - 完成具体代码编写，通过全量单元测试与端到端验证，随特定版本号发布。
5. **现行标准 (Living)**：
   - 成为 Codexen 项目长期遵循的基础架构规范，随着系统与协议演进保持同步更新；
   - 若未来被全新架构体系颠覆，则状态标记为 **Superseded** 并指向新 RFC。

---

## 3. 评审核心准则 (Review Criteria)

在评审一份 RFC 时，评审者必须严格对照以下四项红线：

1. **五大安全不变量合规性**：
   - 方案是否严格遵守沙箱隔离？是否会泄露机密凭据？是否会饿死主事件循环？
2. **Schema 严格性与兼容性**：
   - 工具入参和返回契约是否避免了歧义联合类型（如 `anyOf: [..., null]`）？是否能被各类严格模式的 Agent（如 OpenAI、Claude）无缝调用？
3. **抗回溯与反 DoS 性能**：
   - 正则搜索、文件遍历等操作是否存在无界内存占用或 CPU 假死风险？
4. **自动化测试与可验证性**：
   - 提案是否附带明确的测试矩阵与验证命令？是否有对照差分测试保底？

---

## 4. 规范模板参考 (Template Reference)

新增 RFC 提案时，请统一参考 **[docs/rfcs/template.md](../rfcs/template.md)**，保持全项目技术规格书结构的一致性与专业性。
