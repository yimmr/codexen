# Codexen 开发与协作工作流规范 🛠️
### Codexen Development & Contribution Workflow Specification

本文档定义了参与 **Codexen** 项目开发时的环境基准、代码提交规范、分支管理以及提交前验证门禁。所有核心开发者和 AI Agent 必须严格遵循本规范。

---

## 1. 运行环境与依赖管理基线 (Environment & Toolchain)

### 1.1 Python 版本要求
- Codexen 核心引擎强制要求 **Python `>= 3.10`**（推荐 `3.11`、`3.12` 或 `3.14`）。
- 严禁引入 Python 3.10 以下不支持的语法（如老式类型标注语法）。

### 1.2 包管理器：`uv` 优先
- 推荐使用现代化包管理器 **`uv`**，确保虚拟环境干净、启动开销最小化：
  ```bash
  # 创建并激活环境
  uv venv
  .venv\Scripts\activate   # Windows
  # source .venv/bin/activate # Linux / macOS

  # 安装可编辑依赖
  uv pip install -e .
  ```
- 在执行测试与自动化脚本时，强制添加 `--frozen` 标志，确保依赖锁死，杜绝漂移：
  ```bash
  uv run --frozen python -m unittest discover tests -v
  ```

---

## 2. Git 分支管理模型 (Branching Model)

项目采用基于主干的简化 Git Flow 模型：

| 分支类型 | 命名格式 | 说明与生命周期 |
| :--- | :--- | :--- |
| **主干分支** | `main` | 生产稳定分支。任何合并至 `main` 的代码必须处于可随时部署、通过全量测试的状态。 |
| **功能特性** | `feat/<feature-name>` | 开发重大新特性或演进阶段（如 `feat/hitl-auth`）。 |
| **缺陷修复** | `fix/<bug-name>` | 修复已知 Issue 或安全漏洞（如 `fix/path-normalization`）。 |
| **文档与规范** | `docs/<topic>` | 演进文档、规范库与使用指南更新（如 `docs/standards-reorg`）。 |
| **重构优化** | `refactor/<module>` | 不改变对外接口与行为的代码重构。 |

---

## 3. 提交信息规范 (Conventional Commits)

所有的 Git Commit 信息必须严格遵循 [Conventional Commits](https://www.conventionalcommits.org/) 规范，确保变更日志（Changelog）清晰可追溯。

### 3.1 格式结构
```text
<type>(<scope>): <subject>

[optional body]

[optional footer(s)]
```

### 3.2 允许的 Type 动词
- **`feat`**：新增功能特性（例如新增 MCP 工具、新配置项）；
- **`fix`**：修复缺陷（例如修复路径穿透漏洞、修正 Schema 导出）；
- **`docs`**：仅文档、注释或工程规范变更；
- **`test`**：新增或修改测试用例，未改变业务代码；
- **`refactor`**：代码重构（既不修复 bug 也不添加功能）；
- **`perf`**：性能优化（如正则预编译、Worker 线程池调优）；
- **`build`**：构建系统、依赖管理配置变更（如 `pyproject.toml`、`uv.lock`）；
- **`chore`**：日常辅助任务、配置模板微调或 Git 忽略策略调整。

### 3.3 示例
- 正确：`feat(engine): support dynamic gitignore precedence with pathspec`
- 正确：`fix(fs): normalize leading slashes in relative paths to prevent false security errors`
- 正确：`docs(standards): add comprehensive development workflow and git conventions`
- 错误：`update code`（缺少类型和上下文）
- 错误：`fixed bug`（模糊不清）

---

## 4. 提交前自动化验证门禁 (Pre-commit Verification Gate)

在将代码合并或推送到远端之前，**必须**在本地通过全量单元测试与安全回归测试：

```bash
uv run --frozen python -m unittest discover tests -v
```

### 门禁达标准则：
1. **测试通过率 100%**：当前套件包含 170+ 项单元测试，必须全部通过（`OK`），允许 `0` 项失败（Failures）与 `0` 项错误（Errors）。
2. **安全回归零容忍**：所有敏感凭据防护（`.env*`、私钥、`oauth_store.db`）与路径穿透测试必须全部显式通过。
3. **主事件循环非阻塞验证**：`tests/test_async_search_concurrency.py` 必须顺利通过，确保耗时 I/O 不拖垮主事件循环。

---

## 5. 开源与路径安全准则 (Clean Repository & Path Safety)

为了保护开发者本地隐私并保障跨平台移植性：
1. **禁止硬编码本地私有路径**：
   - 严禁在公开代码或文档中出现形如 `D:\works\...`、`C:\Users\...` 的本地开发机绝对物理路径；
   - 跨工程依赖统一采用相对路径、环境变量或标准包依赖形式。
2. **公私文档清晰解耦**：
   - **公开同步**：工程架构规范（`docs/standards/`）、架构演进路线图（`docs/evolution-roadmap.md`）、架构技术规格书（`docs/rfcs/`）；
   - **本地私有**：具体的 Sprint 开发细案、未公开的试验任务拆解统一归档至 `docs/plans/`；该目录已由 `.gitignore` 全局忽略，切勿强制提交。
