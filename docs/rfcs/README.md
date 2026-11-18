# Codexen 架构规格书库 (RFCs) 📑
### Codexen Requests for Comments Library

本目录归档了 **Codexen** 系统的正式技术规格书（RFC）。每一份 RFC 都代表了经充分论证、通过代码评审并冻结对外契约的核心架构方案。

---

## 🧭 RFC 归档列表 (RFC Index)

| 编号 | 标题 | 状态 | 适用版本 | 提出日期 | 核心主题 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **[RFC 0001](0001-resilient-async-and-adaptive-search.md)** | **弹性异步事件循环、自适应规则分层与防幻觉态势感知** | **Accepted / Living** | `>= 0.2.0` | 2026-11-15 | Worker 线程池非阻塞保底、项目 `.gitignore` 优先、HITL 授权穿透、分页自愈元数据 |
| **[RFC 0002](0002-universal-document-extraction.md)** | **通用文档智能解析引擎与防溢出分段自愈协议** | **Implemented** | `>= 0.4.0` | 2026-11-16 | DOCX/PDF/XLSX/PPTX 提取、read_file 透明路由、Markdown 转换、结构大纲探测、防截断自愈 |
| **[RFC 0003](0003-edit-collaboration-semantics.md)** | **编辑回执与换行/日志可靠性** | **Implemented** | `>= 0.5.0` | 2026-11-18 | 变更行号与 all-or-nothing 可选 hunk 回执、Windows 占用重试；v0.6.0 增加换行一致性策略、CRLF 原子 token 匹配、日志降噪过滤与定位诊断 |
| 📝 **[RFC 编写模板](template.md)** | **标准 RFC 撰写模板** | **Living** | - | - | 新增 RFC 提案时的标准结构与编写指南 |

---

## 📜 流程与指引

- 关于 RFC 的完整生命周期（Draft $\rightarrow$ Review $\rightarrow$ Accepted $\rightarrow$ Implemented $\rightarrow$ Living）以及评审标准，请参阅 **[docs/standards/rfc-process.md](../standards/rfc-process.md)**。
- 提出新的架构演进规格时，请复制 `template.md`，按流水号递增命名（例如 `0002-your-topic.md`），并提交评审。
