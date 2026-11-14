# Codexen MCP Engine 部署与运行指南 (Deployment Guide)

---

## 0. 架构概览与运行契约 (Architecture & Contract)

Codexen 生产环境采用 **两层域名解耦架构**：

```text
[ChatGPT / Claude / Gemini 客户端]
        │ (公网 HTTPS 接入)
        ▼
[第一层：Cloudflare MCP Portal 门户网关] ──> 域名: MCP_PORTAL_URL (如 https://mcp.yourdomain.com/mcp)
        │ (Cloudflare Access 拦截，通过邮箱验证码 / SSO 授权放行)
        │ (Portal 在边缘将客户端请求转换为携带 Bearer Token 的回源请求)
        ▼
[第二层：Cloudflare Tunnel Ingress 域名] ──> 域名: MCP_UPSTREAM_URL (如 https://codexen.yourdomain.com/mcp)
        │ (纯出站加密穿透，无需公网 IP)
        ▼
[本地 cloudflared 守护进程]
        │ (本机回环转发)
        ▼
[本地 Codexen 引擎 (127.0.0.1:8788)]    ──> 校验请求头中的 Bearer <MCP_API_KEY>
```

### 自动化执行契约
```yaml
agent_execution_contract:
  project_name: "codexen"
  project_type: "mcp-server"
  transport: "Streamable HTTP"
  python_requirement: ">=3.10"
  recommended_package_manager: "uv"
  default_port: 8788
  default_mcp_path: "/mcp"
  health_endpoint: "http://127.0.0.1:8788/health"
  start_command: "start.bat"           # Windows 批处理或 uv run launcher.py
  stop_command: "stop.bat"             # 或 uv run launcher.py --stop
  test_command: "uv run python -m unittest discover tests -v"
  isolation_rule: "Workspace root and projects must NEVER overlap with Codexen engine directory."
```

---

## 1. 环境准备与依赖安装 (Prerequisites & Installation)

### 1.1 系统与运行时要求
* **操作系统**：Windows 10/11（推荐，提供原生系统托盘）、Linux（Ubuntu 20.04+）、macOS。
* **Python**：`>= 3.10`（推荐 `3.11` / `3.12` / `3.14`）。
* **包管理器**：推荐使用 **`uv`**（速度极快且虚拟环境隔离干净），亦可使用标准 `pip`。
* **穿透工具**：项目根目录下 `bin/cloudflared.exe`（已内置）或系统 PATH 中的 `cloudflared`。

### 1.2 依赖安装命令
在项目根目录下执行：

```bash
# 优先使用 uv 同步安装全部运行时与测试依赖（自动从 PyPI 拉取 mcp-server-core 核心库）
uv sync --extra test || uv pip install -e ".[test]" || pip install -e ".[test]"
```

> 💡 **架构解耦与跨机部署说明**：  
> Codexen 的底层 Streamable HTTP、OAuth 2.1 与 Cloudflare 隧道守护引擎已解耦为独立的开源核心包 [mcp-server-core](https://pypi.org/project/mcp-server-core/)（GitHub: [yimmr/mcp-framework](https://github.com/yimmr/mcp-framework)）。  
> • **全新部署 / 他人使用**：直接执行上述 `uv sync` 命令即可，包管理器会自动从 PyPI 官方源拉取安装，**完全不依赖任何本地路径**。  
> • **本地联合开发 (开发机专用)**：若需在本地修改框架源码并让项目实时生效，只需在开发机执行 `uv pip install -e <本地mcp-server-core源码路径>` 即可秒级开启热更新联动。

---

## 2. 本地配置初始化 (Local Configuration)

系统配置由 **网络与凭据 (`.env`)** 和 **工作区与权限 (`config.yaml`)** 两部分组成。

### 2.1 初始化环境变量 (`.env`)
复制模板生成 `.env`：
```bash
cp .env.example .env
```

`.env` 核心配置项说明：

```ini
# ==============================================================================
# 1. Cloudflare 命名隧道 Token (在 Cloudflare Networks -> Tunnels 获取)
# ==============================================================================
CLOUDFLARE_TUNNEL_TOKEN=eyJhIjoi...

# ==============================================================================
# 2. 本地回源安全鉴权密钥 (Cloudflare MCP Server 回源调用时携带的 Bearer Token)
# 若初次启动时留空，系统将自动生成高强度密钥 (sec_*) 并自动写回 .env
# ==============================================================================
MCP_API_KEY=sec_your-ultra-secure-mcp-api-key
MCP_REQUIRE_AUTH=true

# ==============================================================================
# 3. 网络与服务端点 (两层域名分工)
# ==============================================================================
MCP_HOST=127.0.0.1
MCP_PORT=8788

# 【对外门户端点】面向 ChatGPT / Claude 等客户端填写的统一访问域名
MCP_PORTAL_URL=https://mcp.yourdomain.com

# 【内部回源端点】Cloudflare Tunnel Ingress 映射到本机 127.0.0.1:8788 的公网域名
MCP_UPSTREAM_URL=https://codexen.yourdomain.com/mcp

# 内部自主轻量 OAuth 2.1 开关 (生产使用 CF 门户鉴权，保持默认 false 即可)
MCP_ENABLE_PUBLIC_OAUTH=false
```

### 2.2 工作区与权限配置 (`config.yaml`)
`config.yaml` 定义允许访问的本地工作区目录与操作权限：
```yaml
workspace:
  root: "D:\works"                # 统一工作区根目录 (严禁与 Codexen 引擎目录重叠!)
  allow_create_project: true

projects:
  my_project:
    root: "D:\works\my_project"
    description: "业务工程"
    read_only: false               # true 为只读项目

permissions:
  allow_file_read: true
  allow_file_write: true
  allow_file_delete: true
  allow_command_execution: true    # 受控终端命令执行
  command_whitelist:               # 白名单直接放行
    - "git"
    - "pytest"
    - "python"
    - "npm"
    - "uv"
  command_require_confirmation:    # 敏感操作需向用户确认并传入 confirmed=True
    - "rm"
    - "del"
    - "git push"
    - "git reset --hard"
```

---

## 3. Cloudflare 边缘与隧道配置 (Cloudflare Zero Trust Setup)

> 💡 **自动化配置提示**：若使用 AI Agent 自动配置 Cloudflare，环境需预先接入 Cloudflare 官方 MCP (`cloudflare-mcp`)，以便通过 API 自动执行以下步骤。

若通过 Cloudflare Zero Trust 控制台手动配置，按以下三步进行：

### 步骤 1：配置命名隧道 Ingress (穿透回源)
1. 登录 [Cloudflare Zero Trust 控制台](https://one.dash.cloudflare.com/) -> **Networks** -> **Tunnels**。
2. 创建或选择命名隧道（如 `mcp-tunnel`），获取客户端 Token，填入 `.env` 的 `CLOUDFLARE_TUNNEL_TOKEN`。
3. 在隧道的 **Public Hostname** 中添加回源规则：
   * **Subdomain**: 如 `codexen`
   * **Domain**: 您的托管域名（如 `yourdomain.com`）
   * **Path**: 留空
   * **Type**: `HTTP`
   * **URL**: `localhost:8788`
4. 保存后，内部穿透回源域名即为 `https://codexen.yourdomain.com/mcp`。

### 步骤 2：注册 MCP Server (服务定义)
1. 在 Zero Trust 控制台导航至 **Access** -> **AI Controls** -> **MCP Servers**。
2. 点击 **Add MCP server**：
   * **Server name / ID**: `codexen`
   * **HTTP URL**: 填入回源端点：`https://codexen.yourdomain.com/mcp`
   * **Authentication**: 选择 **Bearer**
   * **Bearer Token**: 填入 `.env` 中的 `MCP_API_KEY`
3. 保存并连接。边缘节点握手成功后状态变为 **Ready**，并列出 Codexen 提供的 40+ MCP 工具。

### 步骤 3：创建与配置 MCP Portal (客户端接入与 OAuth 鉴权)
1. 切换至 **AI Controls** -> **MCP Portals** 标签页，点击 **Create portal**：
   * **Portal name**: 如 `meocox-mcp`
   * **Portal domain / Hostname**: 填入对外门户域名（如 `mcp.yourdomain.com`）
   * **MCP servers**: 勾选添加上一步创建的 `codexen` 服务。
2. **OAuth 配置 (DCR)**：
   * 开启 **Dynamic Client Registration (DCR)**。
   * **Allowed Redirect URIs** 确保包含：
     * `https://chatgpt.com/*`
     * `https://chatgpt.com/connector_platform_oauth_redirect`
     * `https://chatgpt.com/api/mcp/auth_callback`
     * `https://claude.ai/*`
     * `https://claude.ai/api/mcp/auth_callback`
     * `https://*.google.com/*`
     * `https://*.googleusercontent.com/*`
     * `https://cloud.google.com/*`
     * `https://*.cloud.google.com/*`
     * `https://google.dev/*`
     * `https://*.google.dev/*`
3. **令牌续期与会话生命周期配置（防频繁手动重新授权 - 黄金实践）**：
   * **背景**：Cloudflare 默认的 Refresh Token 有效期仅 24 小时、Access Token 仅 15 分钟。若保持默认，Gemini 等客户端在隔夜或闲置后会判定 Token 死亡，要求重新手动走弹窗授权。
   * **推荐参数**（在 Zero Trust 对应 Access Application 或 API 的 `oauth_configuration.grant` 与应用设置中配置）：
     * **Grant Session Duration**：设置为 `730h`（30 天 / 1 个月，平台上限）。使 Refresh Token 具备整月静默续期能力。
     * **Application Session Duration**：同步设为 `730h`（30 天）。
     * **Access Token Lifetime**：设为 `2h`（2 小时）。平衡会话平滑度与权限即时收回能力，切忌无脑拉满至数月。
4. **Access 访问策略 (Policies)**：
   * 添加 **Allow** 策略，Include 填入您的管理员邮箱，确保只有您本人可凭邮箱验证码通过鉴权。
5. 保存 Portal。对外统一接入端点即为 `https://mcp.yourdomain.com/mcp`。

---

## 4. 启动服务与状态自检 (Start & Verification)

### 4.1 启停方式
* **Windows**：
  * **后台托盘运行**：双击运行 `start.vbs`（右下角托盘图标驻留，无命令行黑框）。
  * **前台控制台调试**：直接运行 `start.bat`（终端实时打印监听地址、公网回源端点与密钥）。
* **CLI / Linux / macOS**：
  * 启动：`uv run launcher.py`
* **停止服务**：
  * 运行 `stop.bat` 或执行 `uv run launcher.py --stop`。

### 4.2 服务健康自检
服务启动后，执行以下命令验证状态：

```bash
# 1. 本地服务健康检查
curl -s http://127.0.0.1:8788/health
# 期望返回: {"status":"healthy"}

# 2. 公网隧道回源健康检查
curl -s https://codexen.yourdomain.com/health
# 期望返回: {"status":"healthy"}

# 3. 运行全量自动化测试基线 (99 项测试)
uv run python -m unittest discover tests -v
# 期望返回: Ran 99 tests ... OK
```

---

## 5. 客户端接入配置 (Client Integration)

客户端接入分为三种典型模式：

### 模式 A：Cloudflare MCP 门户接入 (生产推荐：ChatGPT / Claude / Gemini)
通过 Cloudflare 边缘门户统一接入，享有 OAuth 2.1 鉴权与 DDoS 安全防护。

* **统一接入端点**：
  ```text
  https://mcp.yourdomain.com/mcp
  ```

#### 1. ChatGPT 接入操作
1. 打开 ChatGPT 对话窗口，在输入框附近找到并点击 **【插件 / 工具】** 图标。
2. 在弹出菜单中点击 **添加按钮下拉菜单**。
3. 选择 **【添加自定义 MCP 服务器】**。
4. 配置信息：
   * **名称**：`Codexen Engine`
   * **URL**：`https://mcp.yourdomain.com/mcp`
   * **身份验证**：选择 **OAuth**
5. 点击创建保存。浏览器将自动弹出 Cloudflare Access 验证窗口，输入管理员邮箱验证码完成登录即可开始使用。

#### 2. Claude / Claude Desktop 接入操作
1. 在 Claude 网页端或客户端中打开 **Settings** -> **Connectors** -> **Add custom connector**。
2. 填入 Server URL：`https://mcp.yourdomain.com/mcp`。
3. 按照提示完成 Cloudflare Access 邮箱验证授权。

#### 3. Gemini 接入操作
1. 打开 Gemini 网页端，点击 **【更多】** -> **【自定义】** -> **【关联应用】**。
2. 点击 **【添加自定义】**。
3. 填入 Server URL：`https://mcp.yourdomain.com/mcp`，身份验证选择 **OAuth**。
4. 按照提示完成 Cloudflare Access 邮箱验证授权。

---

### 模式 B：隧道直连 / 本地网络 Bearer 模式 (Cursor / 自动化脚本)
用于不需要 OAuth、直接通过 API Key 直连的本地或远程客户端：

在 Cursor MCP 设置或 `claude_desktop_config.json` 中配置：
```json
{
  "mcpServers": {
    "codexen": {
      "url": "https://codexen.yourdomain.com/mcp",
      "headers": {
        "Authorization": "Bearer sec_your-mcp-api-key"
      }
    }
  }
}
```
*(本机同一局域网内亦可直接填 `http://127.0.0.1:8788/mcp`)*

---

### 模式 C：纯本地 STDIO 模式 (零网络暴露)
无需启动任何 HTTP 端口与网络穿透，利用进程标准输入输出流与本地 IDE 通信：

```json
{
  "mcpServers": {
    "codexen": {
      "command": "uv",
      "args": ["run", "python", "-m", "app", "--stdio"],
      "cwd": "D:/Codexen"
    }
  }
}
```

---

## 6. 常见故障诊断与自愈指南 (Troubleshooting & FAQ)

| 故障现象 | 根本原因 (Root Cause) | 确定性自愈指令 / 解决方案 |
| :--- | :--- | :--- |
| **浏览器打开 `/mcp` 报 `invalid token` 或 401** | **正常机制**。`/mcp` 是流式协议端点，只响应携带 MCP 协议头或合规 OAuth Token 的客户端请求 | 浏览器切勿直接打开 `.../mcp`。若需查看连接说明或登出，请在浏览器直接访问 Portal 首页（根路径 `https://mcp.yourdomain.com/`）。 |
| **Cloudflare 后台 MCP Server 显示 Error 或无法同步工具** | 1. 本地 Codexen 未启动；<br/>2. 穿透域名未解析或隧道离线；<br/>3. MCP Server 填写的 Bearer Token 与 `.env` 中的 `MCP_API_KEY` 不匹配 | **四步排查链**：<br/>1. 检查本地 `curl http://127.0.0.1:8788/health` 是否返回 healthy；<br/>2. 检查 `curl https://codexen.yourdomain.com/health` 是否可达；<br/>3. 检查 Cloudflare MCP Server 的 HTTP URL 必须包含末尾 `/mcp`；<br/>4. 检查 Cloudflare 填写的 Bearer Token 是否与当前 `.env` 中的 `MCP_API_KEY` 逐字一致。 |
| **端口 8788 被占用 (Address already in use)** | 上次服务未完全退出或残留僵尸 PID | 运行 `stop.bat`，或执行 `uv run launcher.py --stop`，系统会自动定位并终止残留进程。 |
| **`SecurityBoundaryViolation`** | `workspace.root` 或挂载项目路径包含了 Codexen 引擎代码自身 | 检查 `config.yaml`，确保所有挂载项目均位于外部目录（如 `D:\works`），严禁挂载 `D:\Codexen` 自身作为被开发项目。 |
| **多个本地服务共用同一个 Cloudflare 隧道 (如 8787 和 8788)** | 同一台机器运行了多个 MCP 服务，但只想拉起一个 `cloudflared` 进程 | `mcp_server_core/tunnel.py` 内置了隧道守护单例逻辑：通过本地 `127.0.0.1:20245/config` 自动感知所有 ingress 端口；只要有任意服务需要，隧道就会保持运行，无需重复启动。 |
| **命令执行返回 `needs_confirmation`** | Agent 触发了敏感高危指令（如 `del`, `rm`, `git push`） | 属于正常人机协同保护逻辑。Agent 需向人类询问确认后，再次发起调用并带上 `confirmed=True` 参数。 |
| **Gemini 接入提示“必须关联账号才能使用此自定义应用。请重试”** | 1. 偶发 OAuth 握手超时或网络抖动；<br/>2. 浏览器内多 Google 账号 Cookie 串扰；<br/>3. CF 策略未放行当前邮箱；<br/>4. 该 Google 账号为不支持 Connected Apps 的企业/学校号 | 1. 重新输入 URL 再发起一次连接；<br/>2. 使用无痕隐身窗口仅登录目标 Google 账号进行授权；<br/>3. 检查 Cloudflare Access 策略 Include 白名单是否包含当前登录邮箱；<br/>4. 确认使用个人 Google 账号且开启了“保留活动记录”。 |
| **Gemini 使用一天或半天后频繁失效，需重新点击 `+` 授权** | Cloudflare 默认 `grant.session_duration` 仅 24 小时，且 Gemini 属于懒惰式加载，闲置隔夜后 Refresh Token 彻底失效，触发客户端强制断开 | 参考第 3 节配置，将 Cloudflare Access 的 `oauth_configuration.grant.session_duration` 与应用 `session_duration` 提升至 `730h`（30 天），并将 `access_token_lifetime` 设为 `2h`。完成后在 Gemini 端重新绑定一次即可享受 30 天无感静默续期。 |

---

## 7. Linux 生产环境 Systemd 守护进程示例 (可选)

对于在 Linux 服务器或无桌面远程开发机上长期运行 Codexen 的场景：

```ini
# /etc/systemd/system/codexen.service
[Unit]
Description=Codexen MCP Engine
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/codexen
EnvironmentFile=/opt/codexen/.env
ExecStart=/home/ubuntu/.cargo/bin/uv run python launcher.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

激活并启动服务：
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now codexen.service
sudo systemctl status codexen.service
```
