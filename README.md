# Web MCP Gateway — 最强 Web MCP 调用本地子代理方案

把 **Web AI 客户端**（ChatGPT 网页版 / Claude.ai / Grok ...）变成能直接操作你本机代码仓库的**编程子代理**：Web 端出脑子，本地出双手。

本仓库将社区三个互补项目统一为一套开箱即用的方案：

| 原项目 | 在统一方案中的角色 | 统一后改进 |
| --- | --- | --- |
| [xyTom/coding-tools-mcp](https://github.com/xyTom/coding-tools-mcp)（PyPI） | 本地子代理运行时：18 个工具（文件读写/多文件补丁/PTY 命令执行/git），workspace 沙箱，OAuth 2.1 + PKCE + RFC 7591 动态注册 | 直接复用 PyPI 版，不改动 |
| CodingToolsMcpLauncher（社区启动器） | 控制面：Web 仪表盘管理 MCP + 隧道进程 | 重写为 `gateway.py`：Windows 原生进程管理、适配 0.3.0 CLI（旧版 `--tool-profile` 已被上游移除）、新增 Bearer/无认证模式、权限模式选择、cloudflared 自动安装、**远端服务器(relay)模式** |
| chatgpt-mcp-autoapproval（Chrome 扩展） | Web 端辅助：授权弹窗自动批准 | 升级为多站点版：支持 ChatGPT / Claude / Grok，默认信任清单对齐 0.3.0 的 18 个真实工具名 |

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│  Web AI 客户端层                                              │
│  ChatGPT 网页版 · Claude.ai · Grok · 任何支持 MCP 的 Web 端   │
│    └─ chrome-extension/：授权弹窗高亮 + 信任清单自动批准        │
├─────────────────────────────────────────────────────────────┤
│  通道层（三种模式任选）                                         │
│  ① 快速隧道   cloudflared quick tunnel → *.trycloudflare.com  │
│  ② 固定域名   自有 Cloudflare 账号 named tunnel / ZT token     │
│  ③ 远端服务器 直接连接已部署的中继（如 https://xxx/mcp）         │
├─────────────────────────────────────────────────────────────┤
│  控制面  gateway.py（http://127.0.0.1:8766）                  │
│  进程监督 · 凭据生成/展示 · 隧道 URL 自动提取 · 日志流 · 健康检查 │
├─────────────────────────────────────────────────────────────┤
│  运行时层  coding-tools-mcp（PyPI，>= 0.3.0）                  │
│  read_file / apply_patch / exec_command / git_* ...          │
│  workspace 沙箱 + safe/trusted/dangerous 权限门控             │
└─────────────────────────────────────────────────────────────┘
```

## 快速开始（Windows）

```powershell
cd web-mcp-gateway
powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1   # 装 Python + cloudflared + coding-tools-mcp
python .\gateway.py                                            # 打开 http://127.0.0.1:8766
```

macOS / Linux：

```bash
pip install coding-tools-mcp && brew install cloudflared   # 或对应包管理器
python3 gateway.py
```

## 三种连接模式

### ① 快速隧道（默认，立即体验）

控制台选择「快速隧道」→ 选工作区 → 启动。自动生成 `https://随机名.trycloudflare.com/mcp` 和 OAuth 密码，两者复制粘贴到 Web 客户端即可。注意：域名每次重启会变。

### ② 固定域名（自有 Cloudflare，日常主力）

1. Cloudflare Zero Trust → Networks → Tunnels → 创建 Tunnel，复制 Token；
2. 控制台选「固定域名隧道」，粘贴 Token（或已 `cloudflared tunnel login` 创建的 named tunnel 名称）；
3. 在 Cloudflare 侧把你的域名（如 `mcp.example.com`）路由到该 Tunnel 的 `http://127.0.0.1:8765`；
4. 填写永久域名后，Gateway 会自动固定 OAuth 签发者（`CODING_TOOLS_MCP_SERVER_URL`），Web 客户端重连无需重复授权。

### ③ 远端服务器（不折腾）

控制台选「远端服务器」，填中继地址（例：`https://sg4.yyjeqhc.cn/mcp`）→ 检测在线 → 复制地址到 Web 客户端。本机不启动任何进程；服务器重启后点「检测」刷新状态。

## Web 客户端接入

| 客户端 | 路径 | 认证 |
| --- | --- | --- |
| ChatGPT 网页版 | 设置 → 连接器(Connectors) → 开发者模式 → 添加 MCP 服务器，粘贴 `/mcp` 地址 | OAuth（首次弹授权页，粘贴 Gateway 显示的密码） |
| Claude.ai | 设置 → Connectors → 添加自定义连接器 | OAuth |
| Grok / 其他 | 各自的 MCP / 连接器入口 | OAuth 或 Bearer（API 型客户端） |

接入后配合 `chrome-extension/`：

```text
chrome://extensions → 开发者模式 → 加载已解压的扩展程序 → 选择 chrome-extension 目录
```

- 未信任的 MCP 工具弹窗会红色高亮 + 徽标提示，可点「一律信任此工具」加入白名单；
- 开启自动批准后，白名单内工具的授权弹窗会被**拟人化节奏**自动点击（避免平台 Bot 检测）；
- 快捷键 `Ctrl+Shift+Y` 手动核准当前弹窗；审计日志存于 localStorage（最近 200 条）。

## 网页压缩 · 会话接力（Web 版 Magic Context）

Web 模式下对话在 ChatGPT 服务端，无法像本地 agent 那样在进程内做上下文压缩（参考 [magic-context](https://github.com/cortexkit/magic-context) 的后台压缩思路）。本项目把记忆外置到本地文件，等效实现无限上下文：

1. 控制台「会话接力」卡片 → **初始化工作区接力文件**：在 `AGENTS.md` 注入接力协议（coding-tools-mcp 会自动将其作为 `initialize` 的 `instructions` 透传给模型）+ 创建 `PROGRESS.md` 存档模板（目标/已完成/当前状态/下一步/关键文件）；
2. 长任务中让模型每完成一个里程碑就更新 `PROGRESS.md`（或你说「存档」）；
3. 上下文将满或被路由到弱模型时，开**新窗口** → 点 **复制新窗口交接提示词** → 粘贴发送，模型 1 次 `read_file PROGRESS.md` 即可恢复全部上下文；
4. OAuth Token 已默认延长至 7 天（`CODING_TOOLS_MCP_OAUTH_TOKEN_TTL=604800`），配合固定域名可稳定复用，无需频繁重连。

> 本质：magic-context 做「进程内后台压缩」，Web MCP 做「文件外置 + 新窗口接力」。效果等价，成本是新窗口的 1 次文件读取。

## Automations（ChatGPT 定时任务）

1. 使用**固定域名模式**（自有 Cloudflare Tunnel + 永久域名），获得稳定 MCP 地址（`trycloudflare.com` 临时域名重启即变，不适合 Automations）；
2. 在 ChatGPT 网页版 → Automations → 创建定时任务，提示词中直接调用 MCP 能力，例如：「每晚 9 点通过 MCP 执行 `exec_command: pytest` 并把失败用例汇总发我」；
3. 7 天 Token 保证周内无需重新授权；配合 `chrome-extension` 自动批准，定时任务可无人值守执行。

## 认证与安全

- `oauth`（默认，Web 客户端推荐）：OAuth 2.1 授权码 + PKCE，RFC 7591 动态注册，密码由 Gateway 生成并在控制台展示；
- `bearer`：适合能自定义请求头的 API 型客户端（Claude Code / Codex / 自研 Agent）；
- `noauth`：仅本机调试，**切勿**对公网暴露；
- 权限模式建议 `trusted`（放行网络命令但仍拦截破坏性命令）；`dangerous` 只在 Docker/VM 沙箱内使用；
- Linux 上游支持 Landlock 内核级文件沙箱；Windows/macOS 无内核沙箱，运行不受信代码请使用上游 Docker 镜像。

## 与上游版本兼容性说明

- 针对 `coding-tools-mcp >= 0.3.0`：固定 18 工具目录，无 `--tool-profile`；
- 旧版社区资料（含 linux.do 教程）中的 `--profile full`、`coding-tools-mcp-chatgpt` 等 0.1.x 时代的参数/入口已失效；
- OAuth 密码 stderr 日志格式（`OAuth authorize password: xxx`）保持不变，健康检查逻辑向前兼容。

## 许可与致谢

- 本仓库派生自社区项目 **CodingToolsMcpLauncher** 与 **chatgpt-mcp-autoapproval**，运行时依赖 **[xyTom/coding-tools-mcp](https://github.com/xyTom/coding-tools-mcp)**（Apache License 2.0, Coding Tools MCP Contributors）。
- 依 Apache 2.0 要求保留原版权与许可声明；派生部分同样以 Apache 2.0 提供。
