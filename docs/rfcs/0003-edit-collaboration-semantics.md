# RFC 0003: 编辑回执增强与已知限制文档化（撤销 revision 指纹）
### Edit Receipt Enhancement and Documented Limitations (Revision Guard Withdrawn)

| 属性 | 内容 |
| :--- | :--- |
| **RFC 编号** | 0003 |
| **标题** | 编辑回执增强与已知限制文档化（撤销 revision 指纹） |
| **作者** | Codexen Architecture & Agent Tooling Group |
| **提出日期** | 2026-11-17 |
| **当前状态** | **Implemented / Delivered in v0.5.0** |
| **适用版本** | Codexen >= 0.5.0（暂定，随评审冻结） |
| **关联模块** | `codexen/core/fs.py`, `codexen/tools/file_tools.py`, `tests/test_edit_large_file_streaming.py`, `tests/test_edit_receipt.py`（待建） |

---

## 1. 摘要 (Summary)

Phase 0 已交付并验证：编辑不再受读取体积门槛限制，改为流式重写 + 原子替换 + 字节保真（已有 11 例 / 35 子例测试）。

本 RFC 曾提出以 `revision` 指纹补齐“读后陈旧检测”。经评审与实测，**该机制撤销**：它本质是 `size + mtime_ns + 首尾 64 KB 哈希` 的启发式指纹，对“同大小、还原 mtime、仅改中段”的改动完全漏检，却需要额外一次工具调用与令牌传递，可靠性配不上复杂度。**并发覆盖风险因此明确接受**（见 §5.1），不做任何服务端并发校验。

Rev.4 只保留三项低成本内容，并全部**文档化**：

1. **回执增加变更行号**（`changed_lines`，单次替换才返回，≈+25 tokens）；
2. **可选的全有或全无 hunk**（`include_hunk` 默认 **false**，需要自证时再开，最坏 ≈180 tokens；放不下就**省略**并提示 `git_diff`/`read_file`，绝不返回裁剪过的片段）；
3. **Windows 占用重试**（`os.replace` 瞬时失败最多重试 **6 次**，退避 50–300 ms，仅失败路径 ≤1.05 s）。

同时把评审关心的两个二线问题——**重试不幂等**、**错误缺少机器可读码**——从“一句话带过”升级为独立的已知限制条目（§5.2、§5.3），写明现状、影响、缓解与决定，避免后续实现时被误认为已解决。

---

## 2. 决策记录 (Decision Log)

| 编号 | 决定 | 依据 |
| :--- | :--- | :--- |
| **D1** | **不做 `revision` 指纹**（撤销 Rev.3 的核心） | 实测：中段等长改写 + 还原 mtime → 令牌完全一致，漏检；令牌需 Agent 额外调用 `get_file_info` 获取并原样回传；为不可靠的启发式增加契约面与往返成本，不划算 |
| **D2** | **不做任何并发校验（含全量 SHA-256 CAS）** | 并发覆盖属于“多写者”场景；当前部署以单 Agent 顺序编辑为主，风险由 git/分支/人工流程承担。未来若并发成为真实问题，**直接上全量哈希重新评审**，不重建指纹方案 |
| **D3** | **不重构错误承载方式**（不引入 `status` 状态机） | 现有 `FileServiceError` 文本已可被 LLM 理解；改成结构化状态属契约变更（⚠️ 兼容成本），收益主要在宿主 UI。保留文本并冻结关键词映射（§5.3） |
| **D4** | **不做服务端幂等键**，重试非幂等作为已知限制文档化 | 幂等键需结果缓存 + TTL + 持久化，与单次编辑成本不成比例；失败路径的 `already_applied` 提示留作可选后续（§5.2） |
| **D5** | **hunk 默认关闭**，仅按需返回 | 回执默认只带行号（≈+21 tokens）；确认改动内容时可 `include_hunk=True`，或直接用 `read_file(start_line,end_line)` 读几行（两者成本相近） |
| **D6** | **保留 Windows 占用重试**与**崩溃残留清理** | 前者避免“本地能改、远程偶发失败”；后者已随 Phase 0 补丁实现（见 §5.4），成本可忽略 |

---

## 3. Phase 0 已交付的改善（对照原始实现）

| 维度 | 原始实现（改动前） | 现在 |
| :--- | :--- | :--- |
| 大文件编辑 | 超过 `max_read_chars`（默认 100,000 字符）必然 `FileSizeLimitError`；整文件读入 + 整文件写回 | 流式窗口重写，任意大小可编辑；10.94 MB 实测 33 ms、峰值内存 5.28 MB |
| 失败安全 | `write_text` 原地覆盖，崩溃/磁盘满/解码异常可能留下半截文件 | 同目录临时文件 + `os.replace`；失败路径删除临时文件，原文件不变 |
| 字节保真 | `errors="replace"` 静默替换非法字节；Windows 上 `\n` → `\r\n`（自带 `\r\n` 变 `\r\r\n`） | 严格解码、`newline=""`，CRLF 与多字节原样保留；二进制显式拒绝 |
| 崩溃残留（二线） | 无自动清理，`.codexen_edit_*.tmp` 可能永久残留且可被 `list_files`/`read_file` 看到 | 编辑前机会式清理同目录 1 小时以上的同类残留；活跃临时文件不受影响；无 `.gitignore` 项目由兜底忽略规则隐藏（测试见 `test_orphaned_temp_files_cleaned_but_active_ones_preserved`） |

以上为既有事实，不是本 RFC 的新增提案。

---

## 4. 保留的可选改动

### 4.1 签名

```python
def edit_file_snippet(
    project_id: str = "",
    project_name: str = "",
    relative_path: str = "",
    path: str = "",
    search_text: str = "",
    replace_text: str = "",
    replace_all: bool = False,
    include_hunk: bool = False,   # 新增：默认关闭；True 时回执附带 ≤320 字符 hunk
) -> dict[str, Any]:
    ...
```

### 4.2 成功回执

```json
{
  "status": "success",
  "action": "edited",
  "project": "core-backend",
  "relative_path": "src/service/handler.py",
  "size_bytes": 20480,
  "occurrences_replaced": 1,
  "chars_written": 12,
  "changed_lines": [{ "start": 118, "end": 121 }],
  "hint": "Edit applied at line 118.",
  "modified_at": "2026-11-17T04:30:12+00:00"
}
```

| 字段 | 规则 |
| :--- | :--- |
| `changed_lines` | 仅**实际替换次数为 1** 时返回 1 个 1-based 闭区间（`replace_all=true` 但只命中一次也算）；多次替换**不返回**逐条行号，只给 `occurrences_replaced` |
| `hunk` | 仅 `include_hunk=true` **且**实际替换次数为 1 时尝试返回；**≤320 字符**、上下各 2 行上下文；多次替换不返回 hunk。**全有或全无**：只有完整预览（全部改动行、完整行内容、前后各 2 行上下文）能放下时才返回；任何需要裁剪、限行或硬切的情况一律改为 `hunk_omitted:"exceeds_budget"`，并在 `hint` 中指向 `git_diff` 或 `read_file(start_line,end_line)` 取证——宁可不给，也不给误导性的残片 |
| 默认回执 | ≈278 字符（≈80 tokens）；`include_hunk=true` 时 350–630 字符（≈100–180 tokens），hunk 上限 320 字符 |
| 失败 | 仍抛 `FileServiceError`（不返回状态对象），消息关键词见 §5.3 |

> 说明：`status` 字段只出现在成功回执中、恒为 `success`；失败没有状态对象，与现有的异常语义保持一致（见 §5.3）。

### 4.3 Windows 占用重试

`os.replace` 抛 `PermissionError`/共享冲突（WinError 32/33）时按 50/100/150/200/250/300 ms 退避，**最多重试 6 次**（首次失败后共 7 次尝试，最坏约 1.05 s）；磁盘满、只读等非瞬时错误**立即失败**。仍失败则抛 `FileServiceError`（消息含原始错误），由 Agent 决定是否稍后重试。仅失败路径付出，正常编辑零新增延迟。

---

## 5. 已知限制与明确接受的风险（重点）

> 本节是 Rev.4 的核心：把“不做”的原因、现状与影响写清，避免后续误以为这些语义已经具备。

### 5.1 并发覆盖（不做陈旧读检测）

* **现象**：Agent 读取文件后，用户 IDE 保存或另一个客户端修改同一文件，Agent 的编辑仍会成功并覆盖对方改动（last-writer-wins）。
* **影响**：并发场景下可能**静默丢失**他人修改；回执不会提示冲突。
* **为何不做**：见 D1/D2——廉价指纹不可靠，全量哈希/锁/会话快照的成本与当前使用模式不匹配。
* **缓解**：① 让 Agent 在关键修改前重新 `read_file`；② 用 git 分支/worktree 隔离多 Agent（`git_*` 工具已具备）；③ 人工协调写入窗口。
* **决定**：**接受**。若未来并发成为高频问题，以全量 SHA-256 CAS 重新立项评审。

### 5.2 重试不幂等（已知限制）

* **现状**：编辑成功后若响应在网关/客户端丢失并触发重试，第二次调用会因 `search_text` 已不存在而返回 `FileServiceError: search_text not found in file '...'. Edit aborted.`——与“片段从未存在”**无法区分**。
* **影响**：Agent 可能误判“上次没成功”，进而重复写入或改换写法，造成二次误改或轮次浪费。
* **当前缓解**：
  * 错误消息包含文件相对路径，Agent 可据此重读核对；
  * 工具描述（待补）提示“若收到 not found，先重读确认是否已应用”；
  * 可选后续：`not_found` 时顺带扫描 `replace_text` 是否已存在，在错误信息中附带“可能已应用”的提示（失败路径 +1 遍扫描，成功路径零成本）。**Rev.4 不实现，仅记录**。
* **决定**：**接受并文档化**；不做服务端幂等键（D4）。

### 5.3 错误缺少机器可读码（已知限制）

* **现状**：所有失败以 `FileServiceError` / `FileNotFoundError` 抛出，MCP 侧表现为 `isError` + 文本消息；无 `code`/`status` 字段。
* **影响**：客户端/Agent 只能做字符串匹配；错误分类不稳定，宿主无法据此做差异化 UI（如“冲突”提示）。
* **当前冻结的文本关键词**（变更需走 RFC）：

| 语义 | 现有消息特征 |
| :--- | :--- |
| 未找到 | `search_text not found in file '...'. Edit aborted.` |
| 多义匹配 | `search_text occurs N times in '...'. Specify replace_all=True...` |
| 只读项目 | `Project '...' is configured as read-only...` |
| 二进制 | `the file looks binary (NUL bytes detected)...` |
| 编码错误 | `file is not valid <encoding> text (...)` |
| 空匹配 | `search_text cannot be empty.` |
| 占用/其它 I/O | `Failed to edit file '...': <原始 OSError>` |

* **决定**：**0.5.0 接受现状**，不改成结构化状态（D3）。若后续宿主或客户端需要，优先以**新增 `code` 字段**（成功/失败统一结构，additive）方式引入，并重新评审兼容性。

### 5.4 临时文件与崩溃残留（已缓解）

* 正常/失败路径：`finally` 清理；成功路径由 `os.replace` 消耗。
* 崩溃/强杀：残留 `.codexen_edit_*.tmp`；已实现**编辑前机会式清理**（同目录、>1 小时、仅本前缀），活跃临时文件不受影响。
* 剩余边界：崩溃后该目录不再发生编辑时，残留会保留；有 `.gitignore` 的项目不适用兜底忽略（项目规则优先），残留可能短暂出现在 `list_files`/`search_code`。
* **决定**：**接受**；不引入启动全项目扫描。

### 5.5 ACL/xattr/硬链接

原子替换要求“新文件换旧文件”，因此只保留 mode 位（`copymode`），不保留 POSIX owner/xattr/ACL、Windows 显式 ACE、硬链接。**决定**：**接受并文档化**；未来如出现强 ACL 依赖，再评估可配置的原地写入策略。

### 5.6 无 fsync、无审计、无回滚

* `os.replace` 只保证命名空间原子，不保证断电后数据落盘（不 `fsync`）。
* 无编辑快照、无 JSONL 审计、无 `restore_file_backup`。
* **决定**：**接受**。回滚交给 git；合规审计需求出现时再立项（成本：每次编辑 +1 遍写/磁盘占用）。

### 5.7 临时文件可见性

编辑进行中的 `.codexen_edit_*.tmp` 是目标同目录的隐藏文件，毫秒级窗口；理论上可能触发 `tsc --watch`/`webpack`/`nodemon` 的一次额外构建。**决定**：**接受并文档化**；不迁移到项目根 `.codexen-tmp/`（会引入仓库污染或需自动改 `.gitignore`）。

### 5.8 hunk 省略后的取证路径（git / read_file）

hunk 被省略不代表信息丢失，只是需要一次显式取证：

* **git 仓库且文件已跟踪**：优先 `git_diff` 工具（或 `run_terminal_command("git diff -- <path>")`）获取权威 diff。注意它包含工作区其他未提交改动，且**未跟踪文件默认不出现**（需要 `git add -N`）；
* **非 git / 未跟踪 / 只要片段**：用 `read_file` 读取 `changed_lines` 给出的行区间（前后各留 2 行）；
* 因此 hunk 的定位是“顺手预览”；省略时 Agent 仍能通过 `changed_lines` + 上述两者之一获得真实修改情况。

---

## 6. 成本预算

| 指标 | Phase 0（现状） | Rev.4（默认） | Rev.4 + `include_hunk` |
| :--- | :--- | :--- | :--- |
| 源文件顺序读 | 1 遍 | 1 遍（不变） | 1 遍（不变） |
| 临时文件写 | 1 遍 | 1 遍（不变） | 1 遍（不变） |
| 10.94 MB 编辑耗时 | 33 ms | 33 ms | 33 ms |
| 回执字符 | ≈195 | ≈278 | 完整 ≈350–630（上限 320）；省略 ≈417 |
| 回执 tokens（≈3.5 字符/token） | ≈55 | ≈80 | ≈100–180；省略 ≈120 |
| 额外工具调用 | 0 | 0 | 0 |
| 磁盘常驻 | 0 | 0 | 0 |

回执字段全部来自内存计算（行号来自流式行计数，hunk 来自上下文环形缓冲），**不产生第二次文件读**；Windows 重试只在 `os.replace` 失败时发生。

---

## 7. 验证与验收用例

用户要求：默认回执与 Phase 0 的 I/O 完全一致，仅在需要时多付 token。

* [ ] **`changed_lines` 正确性**：单行替换、跨行替换、插入、删除四种形态，与手工基准行号一致；
* [ ] **多次替换不回行号/ hunk**：`replace_all=true` 命中 1,000 次时无 `changed_lines`、无 `hunk`，`occurrences_replaced=1000`，回执 ≤400 字符；
* [ ] **hunk 默认关闭**：不传 `include_hunk` 时回执不含 `hunk`，且 ≤400 字符；
* [ ] **hunk 按需且全有或全无**：`include_hunk=true` 且仅 1 次替换、完整预览 ≤320 字符时返回精确上下文；单行过长或改动过大放不下时**不返回 hunk**，改为 `hunk_omitted:"exceeds_budget"`，且 `hint` 同时包含 `git_diff` 与 `read_file(start_line=`；
* [ ] **回执预算**：默认 ≤400 字符、含 hunk ≤700 字符（纳入断言）；
* [ ] **重试行为（已知限制的特征化测试）**：成功编辑后原样重试 → 抛 `search_text not found`（明确断言，防止未来悄悄改为沉默成功）；
* [ ] **错误文本稳定性**：§5.3 表格中的关键词逐一断言（避免重构时无意改变）；
* [ ] **Windows 重试**：瞬时失败前 2 次、第 3 次成功；持续失败共尝试 7 次（首次 + 6 次重试）后抛错且原文件不变；非瞬时错误（如磁盘满）立即失败；
* [ ] **崩溃残留**：1 小时以上残留被清理、活跃临时文件保留（已由 Phase 0 测试覆盖）；
* [ ] 既有 Phase 0 用例（大文件、CRLF/多字节、原子性、10 MB 内存 <10 MB）保持通过。

```bash
uv run --frozen python -m unittest discover tests -v
uv run --frozen python -m ruff check codexen tests
```

---

## 8. 后续项（需要时再评审，不属于 0.5.0）

### v0.6.0 换行与日志可靠性补充（已交付于 v0.6.0）

v0.6.0 在保留本 RFC 回执语义的基础上，增加 `newline_mode="auto"|"raw"`、CRLF 原子 token 匹配、预期失败异常分类、有限定位诊断和日志过滤器。经差分 fuzz、参考模型与全量自动化测试套件验证。并发覆盖、fsync、ACL/xattr 和生产历史回放仍属于已知限制。

| 项 | 触发条件 | 届时成本 |
| :--- | :--- | :--- |
| 全量 SHA-256 CAS（替代被撤销的 revision） | 多写者并发成为真实问题 | 每次受保护编辑 +1 遍全量读（10 ms/11 MB） |
| 新增 `code` 字段的结构化错误 | 宿主需要差异化 UI / 客户端需要稳定分类 | 契约变更，需兼容评审 |
| `already_applied` 提示 | 重试困惑成为高频支持问题 | 失败路径 +1 遍扫描 |
| `dry_run` 预览 | 宿主审批流需要“先看后批” | +1 次临时写（随即删除） |
| MCP `ToolAnnotations` | 客户端消费 `destructiveHint` 做审批 | 0 |
| 快照 + 审计 + `restore_file_backup` | 项目无 git / 合规要求 | 每次编辑 +1 遍写 + 磁盘 |
| 单文件多片段批量编辑 | 一轮多处修改成为主要模式 | 0 额外 I/O，契约复杂度 |
| `occurrence_index`（第 N 次匹配） | `replace_all` 误用/歧义频繁 | 0 |

---

## 9. 明确不做

* **`revision` 指纹、全量哈希 CAS、文件锁、服务端会话读快照**：当前均不做，见 D1/D2 与被撤销的 Rev.3；未来如需按 §8 重新评审；
* **服务端幂等键**：见 D4；
* **自动 git 提交 / 自动格式化 / 自动修复**：属上层编排；
* **自动写入 `.gitignore` / `.git/info/exclude`**：不擅自改用户仓库；
* **ACL/xattr 保留、`fsync`、审计、回滚、启动全项目清扫**：见 §5.5–§5.6、§5.4。

---

## 10. 未决问题

1. `include_hunk` 默认值是否长期保持 false，还是对“单次替换 + 短文件”自适应开启；
2. 错误文本关键词的“冻结”是否足以支撑客户端，还是尽早引入 `code`（§8）；
3. 崩溃残留是否需要启动时清扫（当前仅机会式，成本与边界已评估）；
4. 重试提示是否值得在失败路径加一次 `replace_text` 扫描（§5.2）。

---

## 11. 参考资料 (References)

* Claude Code 陈旧读保护行为（本 RFC 已决定**不**实现同类机制，仅记录取舍）：<https://github.com/anthropics/claude-code/issues/3513>
* 官方 `@modelcontextprotocol/server-filesystem` 的 `edit_file`（`dryRun` 与 diff 回执，属后续项）：<https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem/v/2025.3.28>
* MCP `ToolAnnotations`（后续项）：<https://pub.dev/documentation/mcp_server/latest/mcp_server/ToolAnnotations-class.html>
* 本仓库既有规范：`docs/standards/rfc-process.md`、`docs/standards/security-invariants.md`、`docs/rfcs/0001-resilient-async-and-adaptive-search.md`、`docs/rfcs/0002-universal-document-extraction.md`
