#!/usr/bin/env python3
#
# web-mcp-gateway — unified control plane for driving a local coding-tools-mcp
# sub-agent from web AI clients (ChatGPT / Claude / Grok ...).
#
# Derived from CodingToolsMcpLauncher (Apache License 2.0, Coding Tools MCP
# Contributors, https://github.com/xyTom/coding-tools-mcp). Unified additions:
# native Windows process management, coding-tools-mcp >= 0.3.0 CLI, three
# connectivity modes (quick tunnel / named tunnel / remote relay), bearer auth,
# permission-mode selection, relay health probing, cloudflared auto-install.
#
# Pure standard library. Python >= 3.9 to run the panel itself;
# coding-tools-mcp itself requires Python >= 3.11.

import os
import sys
import json
import time
import socket
import signal
import secrets
import shutil
import re
import threading
import collections
import subprocess
import urllib.request
import urllib.error
from urllib.parse import urlparse
import webbrowser
import http.server
import socketserver

APP_NAME = "Web MCP Gateway"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "web-mcp-gateway.state.json")
WEB_SERVER_PORT = 8766

MCP_OUT_LOG = os.path.join(BASE_DIR, "coding-tools-mcp.stdout.log")
MCP_ERR_LOG = os.path.join(BASE_DIR, "coding-tools-mcp.stderr.log")
TUNNEL_OUT_LOG = os.path.join(BASE_DIR, "cloudflared.stdout.log")
TUNNEL_ERR_LOG = os.path.join(BASE_DIR, "cloudflared.stderr.log")

RELAY_PROBE_CACHE_SECONDS = 15
LOG_BUFFER = collections.deque(maxlen=500)
log_lock = threading.Lock()

_relay_cache = {"at": 0.0, "result": None}
_relay_lock = threading.Lock()

RELAY_MARKER = "<!-- web-mcp-gateway:relay -->"
AGENTS_RELAY_SECTION = f"""{RELAY_MARKER}
# Web MCP 会话接力协议（由 Web MCP Gateway 注入）

本工作区通过 Web AI 客户端 + MCP 驱动。对话上下文在客户端侧，无法像本地 agent 那样在进程内压缩。
为对抗长任务导致的上下文膨胀/降智，遵循以下协议：

1. 会话开始：若用户消息包含「继续 / continue / resume / 接力」，先读取 PROGRESS.md，按其中「下一步」继续。
2. 里程碑更新：每完成一个有意义的阶段（或用户说「存档 / checkpoint」），用 apply_patch 更新 PROGRESS.md：
   - 目标 / 已完成 / 当前状态 / 下一步 / 关键文件与命令 / 未解决问题
   - 控制在 80 行以内，超出时压缩旧条目。
3. 上下文纪律：
   - 优先 search_text / list_files / read_file(带 start_line/limit) 定位，不要整读大文件；
   - exec_command 输出用最大字节限制；长输出用 read_output 分页；
   - 不在对话中粘贴大段文件内容，改为引用路径+行号。
4. 用户在新窗口发送的交接提示词会指向 PROGRESS.md —— 这是唯一的跨会话记忆载体。
"""

PROGRESS_TEMPLATE = """# PROGRESS — 会话接力存档

> 由 AI 子代理维护。新会话开始时先读本文件，完成阶段后更新本文件。
> 对应 Web MCP Gateway 的「网页压缩」方案：对话在 ChatGPT 服务端，外置记忆在本地文件。

- 目标：
- 已完成：
- 当前状态：
- 下一步：
- 关键文件：
- 常用命令：
- 未解决问题：
"""

BOOTSTRAP_PROMPT = (
    "继续之前的任务：请调用 MCP 工具 read_file 读取 PROGRESS.md（在工作区根目录），"
    "理解「目标 / 当前状态 / 下一步」，然后直接从「下一步」继续执行。不要重新探索整个仓库，"
    "除非 PROGRESS.md 信息不足。开始前先用一句话复述你理解的任务状态。"
)


def write_ui_log(message):
    timestamp = time.strftime("%H:%M:%S")
    line = f"[{timestamp}] {message}"
    with log_lock:
        LOG_BUFFER.append(line)
    print(line)


HTML_CONTENT = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Web MCP Gateway</title>
<style>
:root {
    --bg-gradient: radial-gradient(circle at 50% 0%, #1a1a2e 0%, #0d0d13 100%);
    --glass-bg: rgba(26, 26, 36, 0.45);
    --glass-border: rgba(255, 255, 255, 0.07);
    --text-main: #f5f5f7;
    --text-muted: #86868b;
    --accent-blue: #0071e3;
    --accent-blue-hover: #0077ed;
    --accent-green: #30d158;
    --accent-yellow: #ffd60a;
    --accent-red: #ff453a;
    --card-bg: rgba(40, 40, 55, 0.3);
    --input-bg: rgba(10, 10, 15, 0.6);
    --border-radius: 14px;
}
* { box-sizing: border-box; margin: 0; padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", Roboto, Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased; }
body { background: var(--bg-gradient); color: var(--text-main); min-height: 100vh;
    display: flex; justify-content: center; align-items: center; padding: 20px; overflow-x: hidden; }
.ambient-glow { position: absolute; width: 600px; height: 600px;
    background: radial-gradient(circle, rgba(0, 113, 227, 0.08) 0%, rgba(0, 0, 0, 0) 70%);
    top: -200px; left: 50%; transform: translateX(-50%); z-index: -1; pointer-events: none; }
.container { width: 100%; max-width: 1150px; background: var(--glass-bg);
    backdrop-filter: blur(25px); -webkit-backdrop-filter: blur(25px);
    border: 1px solid var(--glass-border); border-radius: 20px; padding: 30px;
    box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
    display: flex; flex-direction: column; gap: 22px; position: relative; }
header { display: flex; justify-content: space-between; align-items: center;
    border-bottom: 1px solid var(--glass-border); padding-bottom: 18px; }
h1 { font-size: 1.45rem; font-weight: 700;
    background: linear-gradient(135deg, #ffffff 0%, #a1a1a6 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; letter-spacing: -0.5px; }
.subtitle { font-size: 0.85rem; color: var(--text-muted); margin-top: 4px; }
.status-badge { display: flex; align-items: center; gap: 8px; font-size: 0.9rem; font-weight: 600;
    padding: 6px 14px; border-radius: 20px; background: rgba(255, 255, 255, 0.05);
    border: 1px solid var(--glass-border); }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-muted); transition: all 0.3s ease; }
.status-dot.running { background: var(--accent-green); box-shadow: 0 0 10px var(--accent-green); animation: pulse 2s infinite; }
.status-dot.partial { background: var(--accent-yellow); box-shadow: 0 0 10px var(--accent-yellow); animation: pulse 2s infinite; }
.status-dot.stopped { background: var(--accent-red); }
@keyframes pulse { 0% { transform: scale(1); opacity: 1; } 50% { transform: scale(1.2); opacity: 0.6; } 100% { transform: scale(1); opacity: 1; } }
.mode-tabs { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }
.mode-tab { background: rgba(255, 255, 255, 0.06); border: 1px solid var(--glass-border);
    border-radius: 12px; padding: 12px; cursor: pointer; text-align: center; transition: all 0.2s ease; }
.mode-tab:hover { background: rgba(255, 255, 255, 0.1); }
.mode-tab.active { background: rgba(0, 113, 227, 0.25); border-color: rgba(0, 113, 227, 0.6); }
.mode-tab .tab-title { font-size: 0.95rem; font-weight: 700; }
.mode-tab .tab-desc { font-size: 0.75rem; color: var(--text-muted); margin-top: 3px; }
.main-layout { display: grid; grid-template-columns: 1.05fr 1fr; gap: 22px; }
@media (max-width: 950px) { .main-layout { grid-template-columns: 1fr; } }
.control-panel { display: flex; flex-direction: column; gap: 18px; }
.panel-card { background: var(--card-bg); border: 1px solid var(--glass-border);
    border-radius: var(--border-radius); padding: 20px; display: flex; flex-direction: column; gap: 14px; }
.form-group { display: flex; flex-direction: column; gap: 6px; }
label { font-size: 0.8rem; font-weight: 600; color: var(--text-muted);
    text-transform: uppercase; letter-spacing: 0.5px; }
.input-row { display: flex; gap: 10px; }
input, select { flex: 1; background: var(--input-bg); border: 1px solid var(--glass-border);
    border-radius: 8px; color: var(--text-main); padding: 10px 14px; font-size: 0.95rem;
    outline: none; transition: all 0.2s ease; }
select { appearance: none; cursor: pointer; }
select option { background: #1a1a2e; color: var(--text-main); }
input:focus, select:focus { border-color: rgba(0, 113, 227, 0.5); box-shadow: 0 0 8px rgba(0, 113, 227, 0.2); }
input[readonly] { color: var(--text-muted); cursor: default; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.btn { background: rgba(255, 255, 255, 0.08); color: var(--text-main);
    border: 1px solid var(--glass-border); border-radius: 8px; padding: 10px 16px;
    font-size: 0.9rem; font-weight: 600; cursor: pointer;
    transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    display: inline-flex; align-items: center; justify-content: center; gap: 8px; outline: none; }
.btn:hover { background: rgba(255, 255, 255, 0.14); transform: translateY(-1px); }
.btn:active { transform: translateY(0); }
.btn-primary { background: var(--accent-blue); border: none; }
.btn-primary:hover { background: var(--accent-blue-hover); box-shadow: 0 4px 12px rgba(0, 113, 227, 0.3); }
.btn-danger { background: var(--accent-red); border: none; }
.btn-danger:hover { background: #ff5247; box-shadow: 0 4px 12px rgba(255, 69, 58, 0.3); }
.action-row { display: grid; grid-template-columns: 2fr 1fr; gap: 14px; }
.hint { font-size: 0.78rem; color: var(--text-muted); line-height: 1.5; }
.info-card { background: var(--card-bg); border: 1px solid var(--glass-border);
    border-radius: var(--border-radius); padding: 20px; display: flex; flex-direction: column; gap: 14px; }
.console-panel { background: rgba(10, 10, 15, 0.85); border: 1px solid var(--glass-border);
    border-radius: var(--border-radius); display: flex; flex-direction: column; overflow: hidden;
    height: 560px; box-shadow: inset 0 0 20px rgba(0, 0, 0, 0.6); }
.console-header { background: rgba(20, 20, 28, 0.8); padding: 12px 20px;
    border-bottom: 1px solid var(--glass-border); display: flex; justify-content: space-between; align-items: center; }
.console-title { font-size: 0.8rem; font-weight: 700; color: var(--text-muted);
    text-transform: uppercase; letter-spacing: 1px; }
.console-body { flex: 1; padding: 20px; overflow-y: auto;
    font-family: "Menlo", "Cascadia Mono", "Courier New", monospace;
    font-size: 0.82rem; line-height: 1.5; color: #d4d4d4;
    display: flex; flex-direction: column; gap: 4px; }
.log-line { white-space: pre-wrap; word-break: break-all; }
.log-mcp-out { color: #30d158; } .log-mcp-err { color: #ff9f0a; }
.log-cf-out { color: #0a84ff; } .log-cf-err { color: #ff453a; }
.toast { position: fixed; bottom: 30px; left: 50%;
    transform: translateX(-50%) translateY(100px); background: rgba(0, 113, 227, 0.9);
    backdrop-filter: blur(10px); color: white; padding: 10px 24px; border-radius: 30px;
    font-size: 0.9rem; font-weight: 600;
    transition: transform 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275);
    box-shadow: 0 10px 25px rgba(0, 0, 0, 0.3); z-index: 1000; }
.toast.show { transform: translateX(-50%) translateY(0); }
</style>
</head>
<body>
<div class="ambient-glow"></div>
<div class="container">
    <header>
        <div>
            <h1>Web MCP Gateway</h1>
            <p class="subtitle">把本地 coding-tools-mcp 子代理安全地交给 Web AI 客户端驱动</p>
        </div>
        <div class="status-badge">
            <div id="statusDot" class="status-dot"></div>
            <span id="statusText">Checking...</span>
        </div>
    </header>

    <div class="mode-tabs" id="modeTabs">
        <div class="mode-tab active" data-mode="quick" onclick="setMode('quick')">
            <div class="tab-title">快速隧道</div>
            <div class="tab-desc">零配置 · trycloudflare 临时域名</div>
        </div>
        <div class="mode-tab" data-mode="named" onclick="setMode('named')">
            <div class="tab-title">固定域名隧道</div>
            <div class="tab-desc">自有 Cloudflare 账号 · 永久地址</div>
        </div>
        <div class="mode-tab" data-mode="relay" onclick="setMode('relay')">
            <div class="tab-title">远端服务器</div>
            <div class="tab-desc">连接已部署的中继 MCP</div>
        </div>
    </div>

    <div class="main-layout">
        <div class="control-panel">
            <div class="panel-card">
                <div class="form-group" data-mode-group="quick,named">
                    <label>工作区 (Workspace)</label>
                    <div class="input-row">
                        <input type="text" id="workspaceInput" placeholder="选择或粘贴本地项目绝对路径">
                        <button class="btn" onclick="browseFolder()">浏览</button>
                    </div>
                </div>

                <div class="grid-2" data-mode-group="quick,named">
                    <div class="form-group">
                        <label>MCP 端口</label>
                        <input type="number" id="portInput" value="8765">
                    </div>
                    <div class="form-group">
                        <label>Metrics 端口</label>
                        <input type="number" id="metricsPortInput" value="20242">
                    </div>
                </div>

                <div class="grid-2" data-mode-group="named">
                    <div class="form-group">
                        <label>Tunnel 名称 / Token</label>
                        <div class="input-row">
                            <input type="text" id="tunnelNameInput" placeholder="tunnel 名称或 Zero Trust Token">
                            <button class="btn" onclick="autoToken()" title="自动创建并获取 Token">自动获取</button>
                        </div>
                    </div>
                    <div class="form-group">
                        <label>永久域名 (可选)</label>
                        <input type="text" id="permanentDomainInput" placeholder="mcp.example.com">
                    </div>
                </div>

                <div class="form-group" data-mode-group="relay">
                    <label>远端 MCP 服务器地址</label>
                    <div class="input-row">
                        <input type="text" id="relayUrlInput" placeholder="https://sg4.yyjeqhc.cn/mcp">
                        <button class="btn" onclick="checkRelay()">检测</button>
                    </div>
                </div>

                <div class="grid-2" data-mode-group="quick,named">
                    <div class="form-group">
                        <label>认证模式</label>
                        <select id="authModeSelect">
                            <option value="oauth" selected>OAuth（Web 客户端推荐）</option>
                            <option value="bearer">Bearer Token（API 客户端）</option>
                            <option value="noauth">无认证（仅本机调试）</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>权限模式</label>
                        <select id="permissionModeSelect">
                            <option value="safe">safe（最严格）</option>
                            <option value="trusted" selected>trusted（日常开发）</option>
                            <option value="dangerous">dangerous（仅容器/VM）</option>
                        </select>
                    </div>
                </div>

                <div class="action-row">
                    <button class="btn btn-primary" id="startBtn" onclick="startServices()">启动 / 重启</button>
                    <button class="btn btn-danger" id="stopBtn" onclick="stopServices()">停止</button>
                </div>
            </div>

            <div class="info-card">
                <div class="form-group">
                    <label>MCP 接入地址</label>
                    <div class="input-row">
                        <input type="text" id="mcpUrlInput" readonly placeholder="未运行">
                        <button class="btn" onclick="copyValue('mcpUrlInput')">复制</button>
                    </div>
                </div>
                <div class="form-group">
                    <label>凭据（OAuth 密码 / Bearer Token）</label>
                    <div class="input-row">
                        <input type="text" id="credentialInput" readonly placeholder="未运行">
                        <button class="btn" onclick="copyValue('credentialInput')">复制</button>
                    </div>
                </div>
                <div class="form-group" data-mode-group="relay">
                    <label>服务器健康状态</label>
                    <div class="input-row">
                        <input type="text" id="relayHealthInput" readonly placeholder="未检测">
                        <button class="btn" onclick="checkRelay()">重新检测</button>
                    </div>
                </div>
                <p class="hint" id="hintBox"></p>
            </div>

            <div class="panel-card" id="contextCard">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <label style="margin:0;">会话接力 · 网页压缩（Web 版 Magic Context）</label>
                    <span id="contextStatus" style="font-size:0.78rem;color:var(--text-muted);"></span>
                </div>
                <p class="hint">对话在 ChatGPT 服务端，无法在进程内压缩。外置记忆到本地文件：<code>AGENTS.md</code> 注入接力协议 + <code>PROGRESS.md</code> 存档。新窗口粘贴交接提示词即可 1 次文件读取恢复上下文。</p>
                <div class="input-row">
                    <button class="btn" style="flex:1;" onclick="initRelay()">初始化工作区接力文件</button>
                    <button class="btn" style="flex:1;" onclick="copyBootstrap()">复制新窗口交接提示词</button>
                </div>
                <p class="hint">配合固定域名 + 7 天 Token，ChatGPT Automations 定时任务也能长期稳定触发，无需频繁重连。</p>
            </div>
        </div>

        <div class="console-panel">
            <div class="console-header">
                <span class="console-title">Live Log Stream</span>
                <button class="btn" style="padding: 4px 10px; font-size: 0.75rem;" onclick="clearLogs()">清空控制台</button>
            </div>
            <div class="console-body" id="consoleBody">
                <div class="log-line" style="color: var(--text-muted);">控制台已初始化，等待 API 连接...</div>
            </div>
        </div>
    </div>
</div>

<div id="toast" class="toast">已复制到剪贴板!</div>

<script>
let currentMode = 'quick';
let statusInterval = null;
let logsInterval = null;

window.onload = () => {
    fetchStatus(true);
    fetchLogs();
    statusInterval = setInterval(() => fetchStatus(false), 2500);
    logsInterval = setInterval(fetchLogs, 1000);
};

function setMode(mode) {
    currentMode = mode;
    document.querySelectorAll('.mode-tab').forEach(t => t.classList.toggle('active', t.dataset.mode === mode));
    document.querySelectorAll('[data-mode-group]').forEach(el => {
        const groups = el.dataset.modeGroup.split(',');
        el.style.display = groups.includes(mode) ? '' : 'none';
    });
    updateHint();
}

function updateHint() {
    const hints = {
        quick: '启动后会生成 trycloudflare.com 临时 HTTPS 域名（每次重启会变）。适合立即体验：把「MCP 接入地址」粘贴到 ChatGPT 连接器 / Claude Connectors 即可。',
        named: '填入 Cloudflare Zero Trust 的 Tunnel Token（长字符串）或已创建的 named tunnel。配置永久域名后 OAuth 签发者将固定，Web 客户端重连无需重新授权。',
        relay: '直接使用远端已部署的 coding-tools-mcp 服务器，本机不启动任何进程。服务器重启后点「检测」刷新状态。'
    };
    document.getElementById('hintBox').textContent = hints[currentMode] || '';
}

function showToast(message) {
    const toast = document.getElementById('toast');
    toast.innerText = message;
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 2000);
}

function copyValue(inputId) {
    const input = document.getElementById(inputId);
    if (input.value && input.value !== "") {
        navigator.clipboard.writeText(input.value)
            .then(() => showToast("已复制到剪贴板!"))
            .catch(() => showToast("复制失败"));
    } else {
        showToast("没有可复制的内容");
    }
}

function clearLogs() {
    document.getElementById('consoleBody').innerHTML = '<div class="log-line" style="color: var(--text-muted);">控制台已清空。</div>';
}

function fetchStatus(forceSyncInputs = false) {
    fetch('/api/status')
        .then(res => res.json())
        .then(data => {
            const dot = document.getElementById('statusDot');
            const text = document.getElementById('statusText');
            const clsMap = { running: 'running', 'relay-up': 'running', partial: 'partial', 'relay-degraded': 'partial' };
            dot.className = 'status-dot ' + (clsMap[data.status] || 'stopped');
            text.innerText = data.statusText || (data.status || '').toUpperCase();

            if (forceSyncInputs && data.mode) {
                setMode(data.mode);
                if (data.workspace) document.getElementById('workspaceInput').value = data.workspace;
                if (data.port) document.getElementById('portInput').value = data.port;
                if (data.metricsPort) document.getElementById('metricsPortInput').value = data.metricsPort;
                if (data.tunnelName !== undefined) document.getElementById('tunnelNameInput').value = data.tunnelName || '';
                if (data.permanentDomain !== undefined) document.getElementById('permanentDomainInput').value = data.permanentDomain || '';
                if (data.relayUrl !== undefined) document.getElementById('relayUrlInput').value = data.relayUrl || '';
                if (data.authMode) document.getElementById('authModeSelect').value = data.authMode;
                if (data.permissionMode) document.getElementById('permissionModeSelect').value = data.permissionMode;
            }

            document.getElementById('mcpUrlInput').value = data.mcpUrl || "";
            document.getElementById('credentialInput').value = data.credential || (data.authMode === 'noauth' ? '无（未启用认证）' : '');
            if (data.relay) {
                document.getElementById('relayHealthInput').value =
                    (data.relay.ok ? '在线 · ' : '离线 · ') + (data.relay.detail || '') +
                    (data.relay.latencyMs !== undefined ? ' · ' + data.relay.latencyMs + 'ms' : '');
            } else {
                document.getElementById('relayHealthInput').value = '未检测';
            }
        })
        .catch(err => {
            console.error("Status check failed:", err);
            document.getElementById('statusText').innerText = "已断开";
            document.getElementById('statusDot').className = "status-dot stopped";
        });
}

let lastLogsLength = 0;
function fetchLogs() {
    fetch('/api/logs')
        .then(res => res.json())
        .then(data => {
            const consoleBody = document.getElementById('consoleBody');
            if (data.logs.length === 0) { lastLogsLength = 0; return; }
            if (data.logs.length > lastLogsLength) {
                const newLines = data.logs.slice(lastLogsLength);
                newLines.forEach(line => {
                    const div = document.createElement('div');
                    div.className = 'log-line';
                    if (line.includes('[MCP_OUT]')) div.classList.add('log-mcp-out');
                    else if (line.includes('[MCP_ERR]')) div.classList.add('log-mcp-err');
                    else if (line.includes('[CF_OUT]')) div.classList.add('log-cf-out');
                    else if (line.includes('[CF_ERR]')) div.classList.add('log-cf-err');
                    div.innerText = line;
                    consoleBody.appendChild(div);
                });
                lastLogsLength = data.logs.length;
                consoleBody.scrollTop = consoleBody.scrollHeight;
            }
        })
        .catch(err => console.error("Log fetch failed:", err));
}

function browseFolder() {
    const currentDir = document.getElementById('workspaceInput').value || "";
    fetch('/api/browse', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ initial_dir: currentDir })
    })
    .then(res => res.json())
    .then(data => {
        if (data.path) {
            document.getElementById('workspaceInput').value = data.path;
            showToast("工作区已更新");
        }
    })
    .catch(() => showToast("系统目录选择失败，请手动粘贴路径"));
}

function startServices() {
    if (currentMode === 'relay') {
        const relayUrl = document.getElementById('relayUrlInput').value.trim();
        if (!relayUrl) { alert("请先填写远端 MCP 服务器地址。"); return; }
        showToast("正在连接远端服务器...");
        fetch('/api/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: 'relay', relayUrl })
        })
        .then(res => res.json())
        .then(data => {
            if (data.success) { showToast("远端模式已启用"); fetchStatus(true); }
            else alert("连接错误: " + data.error);
        })
        .catch(() => alert("网络错误：无法联系 Gateway 后台进程。"));
        return;
    }

    const workspace = document.getElementById('workspaceInput').value.trim();
    if (!workspace) { alert("请先选择或输入工作区目录路径。"); return; }

    showToast("正在启动服务...");
    fetch('/api/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            mode: currentMode,
            workspace,
            port: parseInt(document.getElementById('portInput').value),
            metricsPort: parseInt(document.getElementById('metricsPortInput').value),
            authMode: document.getElementById('authModeSelect').value,
            permissionMode: document.getElementById('permissionModeSelect').value,
            tunnelName: document.getElementById('tunnelNameInput').value.trim(),
            permanentDomain: document.getElementById('permanentDomainInput').value.trim()
        })
    })
    .then(res => res.json())
    .then(data => {
        if (data.success) { showToast("服务启动成功!"); fetchStatus(true); }
        else alert("启动错误: " + data.error);
    })
    .catch(() => alert("网络错误：无法联系 Gateway 后台进程。"));
}

function stopServices() {
    showToast("正在停止服务...");
    fetch('/api/stop', { method: 'POST' })
    .then(res => res.json())
    .then(data => {
        if (data.success) { showToast("服务已停止。"); fetchStatus(true); }
        else alert("停止错误: " + data.error);
    })
    .catch(() => alert("网络错误：无法联系 Gateway 后台进程。"));
}

function checkRelay() {
    const relayUrl = document.getElementById('relayUrlInput').value.trim();
    if (!relayUrl) { showToast("请先填写服务器地址"); return; }
    document.getElementById('relayHealthInput').value = "检测中...";
    fetch('/api/relay-check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ relayUrl })
    })
    .then(res => res.json())
    .then(data => {
        const r = data.relay || {};
        document.getElementById('relayHealthInput').value =
            (r.ok ? '在线 · ' : '离线 · ') + (r.detail || '') +
            (r.latencyMs !== undefined ? ' · ' + r.latencyMs + 'ms' : '');
        showToast(r.ok ? "服务器在线" : "服务器不可达");
    })
    .catch(() => showToast("检测请求失败"));
}

function autoToken() {
    const btn = document.querySelector('button[onclick="autoToken()"]');
    if (btn) { btn.textContent = "获取中..."; btn.disabled = true; }
    fetch('/api/tunnel/auto-token', { method: 'POST' })
    .then(res => res.json())
    .then(data => {
        if (data.token) {
            document.getElementById('tunnelNameInput').value = data.token;
            showToast("Token 已自动填入");
        } else {
            alert(data.error || "获取失败，请运行 setup-fixed-tunnel.ps1 或到 one.dash.cloudflare.com 手动复制");
        }
    })
    .catch(() => alert("获取失败：Gateway 未运行或 cloudflared 未登录"))
    .finally(() => { if (btn) { btn.textContent = "自动获取"; btn.disabled = false; } });
}

function initRelay() {
    const workspace = document.getElementById('workspaceInput').value.trim();
    if (!workspace) { alert("请先选择或输入工作区目录路径。"); return; }
    fetch('/api/context/init', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ workspace })
    })
    .then(res => res.json())
    .then(data => {
        if (data.success) {
            let msg = [];
            if (data.agentsCreated) msg.push("AGENTS.md 已创建");
            else if (data.agentsAppended) msg.push("AGENTS.md 已追加接力协议");
            else msg.push("AGENTS.md 已存在");
            if (data.progressCreated) msg.push("PROGRESS.md 已创建");
            else msg.push("PROGRESS.md 已存在");
            document.getElementById('contextStatus').textContent = msg.join(" · ");
            showToast(msg.join("，"));
        } else {
            alert("初始化失败: " + data.error);
        }
    })
    .catch(() => alert("网络错误：无法联系 Gateway。"));
}

function copyBootstrap() {
    fetch('/api/context/prompt')
    .then(res => res.json())
    .then(data => {
        if (data.prompt) {
            navigator.clipboard.writeText(data.prompt)
                .then(() => showToast("交接提示词已复制"))
                .catch(() => showToast("复制失败"));
        }
    })
    .catch(() => showToast("获取提示词失败"));
}

setMode('quick');
updateHint();
</script>
</body>
</html>
"""


def browse_folder_dialog(initial_dir):
    if not initial_dir or not os.path.exists(initial_dir):
        initial_dir = os.path.expanduser("~")

    if sys.platform == "win32":
        try:
            ps_code = (
                "[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null;"
                "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog;"
                f"$dialog.SelectedPath = '{initial_dir.replace('/', os.sep).replace(chr(39), chr(39)*2)}';"
                "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $dialog.SelectedPath }"
            )
            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-STA", "-Command", ps_code],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            stdout, _ = proc.communicate(timeout=120)
            if proc.returncode == 0:
                path = stdout.decode("utf-8", errors="replace").strip()
                if path:
                    return path
        except Exception as e:
            write_ui_log(f"PowerShell dialog exception: {e}")
    elif sys.platform == "darwin":
        try:
            script = f'POSIX path of (choose folder with prompt "Choose workspace folder to expose" default location "{initial_dir}")'
            proc = subprocess.Popen(["osascript", "-e", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, _ = proc.communicate()
            if proc.returncode == 0:
                path = stdout.decode("utf-8").strip()
                if path:
                    return path
        except Exception as e:
            write_ui_log(f"AppleScript dialog exception: {e}")
    return None


def get_state():
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(**kwargs):
    state = {
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    state.update(kwargs)
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4, ensure_ascii=False)
    except Exception as e:
        write_ui_log(f"Error saving state file: {e}")


def test_process_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", errors="ignore")
            return f'"{pid}"' in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_pid_tree(pid):
    if not pid:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=15,
            )
            write_ui_log(f"Terminated process tree {pid} (taskkill).")
        except Exception as e:
            write_ui_log(f"taskkill failed for {pid}: {e}")
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            write_ui_log(f"Sent SIGTERM to process {pid}.")
        except Exception as e:
            write_ui_log(f"Failed to kill process {pid}: {e}")
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass


def get_listening_pid(port):
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "tcp"], stderr=subprocess.DEVNULL
            ).decode("utf-8", errors="ignore")
            for line in out.splitlines():
                m = re.match(r"\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)", line)
                if m and int(m.group(1)) == port:
                    return int(m.group(2))
        except Exception:
            pass
        return None
    try:
        output = subprocess.check_output(
            ["lsof", "-i", f"tcp:{port}", "-t", "-sTCP:LISTEN"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8").strip()
        if output:
            return int(output.splitlines()[0])
    except Exception:
        pass
    return None


def get_managed_processes_by_port(port):
    mcp_pid = None
    tunnel_pid = None

    if os.name == "nt":
        try:
            ps = (
                "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine} | "
                "Where-Object {$_.CommandLine -match 'coding-tools-mcp|cloudflared'} | "
                "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", errors="ignore")
            entries = json.loads(out) if out.strip() else []
            if isinstance(entries, dict):
                entries = [entries]
            for entry in entries:
                cmd = entry.get("CommandLine") or ""
                pid = entry.get("ProcessId")
                if "coding-tools-mcp" in cmd and f"--port {port}" in cmd:
                    mcp_pid = pid
                elif "cloudflared" in cmd and f"127.0.0.1:{port}" in cmd:
                    tunnel_pid = pid
        except Exception as e:
            write_ui_log(f"Background process scan failed: {e}")
        return mcp_pid, tunnel_pid

    try:
        output = subprocess.check_output(["ps", "-ax", "-o", "pid,command"]).decode("utf-8", errors="ignore")
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            pid_str, cmd = parts
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            if "coding-tools-mcp" in cmd and f"--port {port}" in cmd:
                mcp_pid = pid
            elif "cloudflared" in cmd and f"127.0.0.1:{port}" in cmd:
                tunnel_pid = pid
    except Exception as e:
        write_ui_log(f"Background process scan failed: {e}")
    return mcp_pid, tunnel_pid


def get_password_from_log():
    for log_path in [MCP_ERR_LOG]:
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    match = re.search(r"OAuth authorize password:\s*(\S+)", f.read())
                    if match:
                        return match.group(1)
            except Exception:
                pass
    return ""


def get_tunnel_url_from_metrics(metrics_port):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{metrics_port}/metrics")
        with urllib.request.urlopen(req, timeout=2) as response:
            content = response.read().decode("utf-8", errors="ignore")
            matches = re.findall(r'userHostname="(https://[a-z0-9-]+\.trycloudflare\.com)"', content)
            if matches:
                return matches[-1]
    except Exception:
        pass
    return ""


def get_tunnel_url_from_log(wait_seconds=0):
    deadline = time.time() + wait_seconds
    while True:
        for log_path in [TUNNEL_ERR_LOG, TUNNEL_OUT_LOG]:
            if os.path.exists(log_path):
                try:
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                        match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", f.read())
                        if match:
                            return match.group(0)
                except Exception:
                    pass
        if time.time() >= deadline:
            break
        time.sleep(0.75)
    return ""


def wait_for_local_mcp(port):
    deadline = time.time() + 20
    while time.time() < deadline:
        for probe_path in ("/.well-known/oauth-authorization-server", "/mcp"):
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}{probe_path}")
                with urllib.request.urlopen(req, timeout=2) as response:
                    if response.status in [200, 401, 404, 405]:
                        return True
            except urllib.error.HTTPError:
                return True
            except Exception:
                pass
        time.sleep(0.5)
    return False


def normalize_relay_url(relay_url):
    url = relay_url.strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    return base, url


def probe_relay(relay_url, force=False):
    normalized = normalize_relay_url(relay_url)
    if not normalized:
        return {"ok": False, "detail": "invalid URL"}
    base, full = normalized

    with _relay_lock:
        now = time.time()
        if (not force and _relay_cache["result"] is not None
                and now - _relay_cache["at"] < RELAY_PROBE_CACHE_SECONDS
                and _relay_cache.get("url") == full):
            return _relay_cache["result"]

        t0 = time.time()
        result = None
        last_error = ""
        for probe_path in ("/.well-known/oauth-protected-resource", "/mcp"):
            try:
                req = urllib.request.Request(
                    base + probe_path,
                    headers={"Accept": "application/json, text/event-stream"},
                )
                with urllib.request.urlopen(req, timeout=10) as response:
                    result = {"ok": True, "detail": f"HTTP {response.status} on {probe_path}"}
                    break
            except urllib.error.HTTPError as e:
                result = {"ok": True, "detail": f"HTTP {e.code} on {probe_path}"}
                break
            except Exception as e:
                last_error = str(e) or type(e).__name__
        if result is None:
            result = {"ok": False, "detail": last_error[:160]}
        result["latencyMs"] = int((time.time() - t0) * 1000)

        _relay_cache["at"] = now
        _relay_cache["result"] = result
        _relay_cache["url"] = full
        return result


def find_coding_tools_mcp():
    exe_names = ["coding-tools-mcp"]
    suffix = ".exe" if os.name == "nt" else ""

    for name in exe_names:
        cmd = shutil.which(name)
        if cmd:
            return cmd

    import site
    candidates = []
    candidates.extend([
        os.path.join(BASE_DIR, ".venv", "Scripts" if os.name == "nt" else "bin", "coding-tools-mcp" + suffix),
        os.path.join(BASE_DIR, "venv", "Scripts" if os.name == "nt" else "bin", "coding-tools-mcp" + suffix),
    ])

    try:
        user_base = site.getuserbase()
        if user_base:
            if os.name == "nt":
                candidates.append(os.path.join(user_base, "Python313", "Scripts", "coding-tools-mcp.exe"))
                candidates.append(os.path.join(user_base, "Python312", "Scripts", "coding-tools-mcp.exe"))
                candidates.append(os.path.join(user_base, "Python311", "Scripts", "coding-tools-mcp.exe"))
            else:
                candidates.append(os.path.join(user_base, "bin", "coding-tools-mcp"))
    except Exception:
        pass

    try:
        python_bin_dir = os.path.dirname(sys.executable)
        candidates.append(os.path.join(python_bin_dir, "coding-tools-mcp" + suffix))
    except Exception:
        pass

    home = os.path.expanduser("~")
    if os.name == "nt":
        candidates.extend([
            os.path.join(home, ".local", "bin", "coding-tools-mcp.exe"),
            os.path.join(os.environ.get("APPDATA", ""), "Python", "Python313", "Scripts", "coding-tools-mcp.exe"),
            os.path.join(os.environ.get("APPDATA", ""), "Python", "Python312", "Scripts", "coding-tools-mcp.exe"),
            os.path.join(os.environ.get("APPDATA", ""), "Python", "Python311", "Scripts", "coding-tools-mcp.exe"),
            os.path.join(os.environ.get("PROGRAMFILES", ""), "coding-tools-mcp", "coding-tools-mcp.exe"),
        ])
    else:
        candidates.extend([
            os.path.join(home, ".local", "bin", "coding-tools-mcp"),
            "/opt/homebrew/bin/coding-tools-mcp",
            "/usr/local/bin/coding-tools-mcp",
        ])

    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def init_session_relay(workspace):
    if not os.path.isdir(workspace):
        return {"success": False, "error": f"工作区目录不存在: {workspace}"}
    agents_path = os.path.join(workspace, "AGENTS.md")
    progress_path = os.path.join(workspace, "PROGRESS.md")
    agents_created = False
    agents_appended = False
    progress_created = False
    try:
        if os.path.exists(agents_path):
            with open(agents_path, "r", encoding="utf-8") as f:
                content = f.read()
            if RELAY_MARKER not in content:
                with open(agents_path, "a", encoding="utf-8") as f:
                    if content and not content.endswith("\n"):
                        f.write("\n")
                    f.write("\n" + AGENTS_RELAY_SECTION + "\n")
                agents_appended = True
        else:
            with open(agents_path, "w", encoding="utf-8") as f:
                f.write(AGENTS_RELAY_SECTION + "\n")
            agents_created = True
        if not os.path.exists(progress_path):
            with open(progress_path, "w", encoding="utf-8") as f:
                f.write(PROGRESS_TEMPLATE)
            progress_created = True
    except Exception as e:
        return {"success": False, "error": str(e)}
    return {
        "success": True,
        "agentsCreated": agents_created,
        "agentsAppended": agents_appended,
        "progressCreated": progress_created,
        "prompt": BOOTSTRAP_PROMPT,
    }


def find_cloudflared():
    suffix = ".exe" if os.name == "nt" else ""
    cmd = shutil.which("cloudflared")
    if cmd:
        return cmd

    home = os.path.expanduser("~")
    if os.name == "nt":
        candidates = [
            os.path.join(home, ".local", "bin", "cloudflared.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "cloudflared", "cloudflared.exe"),
            os.path.join(os.environ.get("PROGRAMFILES", ""), "cloudflared", "cloudflared.exe"),
        ]
    else:
        candidates = [
            "/opt/homebrew/bin/cloudflared",
            "/usr/local/bin/cloudflared",
            os.path.join(home, "Downloads", "cloudflared"),
        ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def auto_install_cloudflared():
    if os.name != "nt":
        return None
    target_dir = os.path.join(os.path.expanduser("~"), ".local", "bin")
    target = os.path.join(target_dir, "cloudflared.exe")
    try:
        os.makedirs(target_dir, exist_ok=True)
        write_ui_log("cloudflared 未安装，正在通过 winget 安装...")
        result = subprocess.run(
            ["winget", "install", "--id", "Cloudflare.cloudflared", "-e",
             "--accept-source-agreements", "--accept-package-agreements"],
            capture_output=True, timeout=600,
        )
        found = find_cloudflared()
        if found:
            write_ui_log(f"cloudflared 安装成功: {found}")
            return found
        write_ui_log(f"winget 安装未成功 (exit {result.returncode})，尝试直接下载...")
        ps = (
            "Invoke-WebRequest -UseBasicParsing -Uri "
            "'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' "
            f"-OutFile '{target}'"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=600,
        )
        if os.path.exists(target) and os.path.getsize(target) > 1000000:
            write_ui_log(f"cloudflared 已下载到 {target}")
            return target
    except Exception as e:
        write_ui_log(f"cloudflared 自动安装失败: {e}")
    return None


def stop_managed_services(port=8765, quiet_if_none=False):
    state = get_state()
    if state and state.get("mode") == "relay":
        if os.path.exists(STATE_PATH):
            try:
                os.remove(STATE_PATH)
            except Exception:
                pass
        write_ui_log("Relay mode stopped (no local processes to terminate).")
        return

    if state and "port" in state:
        port = state["port"]

    pids_to_stop = []
    if state:
        if state.get("tunnelPid"):
            pids_to_stop.append(state["tunnelPid"])
        if state.get("mcpPid"):
            pids_to_stop.append(state["mcpPid"])

    mcp_scan, tunnel_scan = get_managed_processes_by_port(port)
    if mcp_scan:
        pids_to_stop.append(mcp_scan)
    if tunnel_scan:
        pids_to_stop.append(tunnel_scan)

    pids_to_stop = list(set(filter(None, pids_to_stop)))

    if not pids_to_stop:
        if not quiet_if_none:
            write_ui_log(f"No matching processes detected on port {port}.")
        if os.path.exists(STATE_PATH):
            try:
                os.remove(STATE_PATH)
            except Exception:
                pass
        return

    for pid in pids_to_stop:
        if test_process_alive(pid):
            kill_pid_tree(pid)

    if os.path.exists(STATE_PATH):
        try:
            os.remove(STATE_PATH)
        except Exception:
            pass


def start_relay_mode(relay_url):
    normalized = normalize_relay_url(relay_url)
    if not normalized:
        return {"success": False, "error": "远端服务器地址不能为空。"}
    base, full = normalized

    stop_managed_services(quiet_if_none=True)

    write_ui_log(f"Relay mode: probing {full} ...")
    health = probe_relay(full, force=True)
    if health.get("ok"):
        write_ui_log(f"Relay server is UP: {health.get('detail')} ({health.get('latencyMs')}ms)")
    else:
        write_ui_log(f"Warning: relay server unreachable: {health.get('detail')}")

    if os.path.exists(STATE_PATH):
        try:
            os.remove(STATE_PATH)
        except Exception:
            pass

    mcp_url = full if full.endswith("/mcp") else base + "/mcp"
    save_state(
        mode="relay",
        relayUrl=full,
    )
    return {
        "success": True,
        "mcpUrl": mcp_url,
        "relay": health,
        "warning": None if health.get("ok") else "服务器当前不可达，可能正在重启。稍后可点「检测」刷新。",
    }


def start_managed_services(mode, workspace, port, metrics_port, auth_mode="oauth",
                           permission_mode="trusted", tunnel_name="", permanent_domain=""):
    if not os.path.isdir(workspace):
        return {"success": False, "error": f"工作区目录不存在: {workspace}"}

    mcp_exe = find_coding_tools_mcp()
    cloudflared_exe = find_cloudflared()

    if not mcp_exe:
        return {
            "success": False,
            "error": "未找到 coding-tools-mcp。请先安装: pip install coding-tools-mcp "
                     "（需要 Python >= 3.11），或运行项目内的 setup-windows.ps1。",
        }
    if not cloudflared_exe:
        write_ui_log("cloudflared 未找到，尝试自动安装...")
        cloudflared_exe = auto_install_cloudflared()
        if not cloudflared_exe:
            return {
                "success": False,
                "error": "未找到 cloudflared 且自动安装失败。请手动安装: "
                         "winget install Cloudflare.cloudflared",
            }

    if mode == "named" and not tunnel_name and not permanent_domain:
        return {
            "success": False,
            "error": "固定域名模式需要填写 Tunnel 名称 / Zero Trust Token（或至少填写永久域名）。",
        }

    existing_pid = get_listening_pid(port)
    if existing_pid:
        state = get_state()
        is_managed = False
        if state and state.get("mcpPid") == existing_pid:
            is_managed = True
        mcp_scan_pid, _ = get_managed_processes_by_port(port)
        if mcp_scan_pid == existing_pid:
            is_managed = True
        if not is_managed:
            return {"success": False, "error": f"端口 {port} 已被进程 PID {existing_pid} 占用。"}

    stop_managed_services(port, quiet_if_none=True)
    time.sleep(1)

    for log_file in [MCP_OUT_LOG, MCP_ERR_LOG, TUNNEL_OUT_LOG, TUNNEL_ERR_LOG]:
        if os.path.exists(log_file):
            try:
                os.remove(log_file)
            except Exception:
                pass

    password = ""
    bearer_token = ""
    env = os.environ.copy()
    env.pop("CODING_TOOLS_MCP_OAUTH_PASSWORD", None)
    env.pop("CODING_TOOLS_MCP_SERVER_URL", None)
    env.pop("CODING_TOOLS_MCP_OAUTH_TOKEN_TTL", None)

    if auth_mode == "oauth":
        password = secrets.token_urlsafe(24).rstrip("=").replace("+", "-").replace("/", "_")
        env["CODING_TOOLS_MCP_OAUTH_PASSWORD"] = password
        env["CODING_TOOLS_MCP_OAUTH_TOKEN_TTL"] = "604800"
    elif auth_mode == "bearer":
        bearer_token = secrets.token_urlsafe(32).rstrip("=")

    if permanent_domain:
        pd = permanent_domain.strip().lower()
        if pd.startswith("https://"):
            pd = pd[8:]
        elif pd.startswith("http://"):
            pd = pd[7:]
        if pd.endswith("/mcp"):
            pd = pd[:-4]
        if pd.endswith("/"):
            pd = pd[:-1]
        permanent_domain = pd
        env["CODING_TOOLS_MCP_SERVER_URL"] = f"https://{permanent_domain}"

    exe_name = os.path.basename(mcp_exe)
    write_ui_log(f"Starting {exe_name} (permission-mode={permission_mode}, auth={auth_mode})...")
    workspace_arg = workspace.replace("\\", "/")
    mcp_args = [
        mcp_exe,
        "--workspace", workspace_arg,
        "--host", "127.0.0.1",
        "--port", str(port),
        "--permission-mode", permission_mode,
    ]
    if auth_mode == "oauth":
        mcp_args.append("--oauth-mode")
    elif auth_mode == "bearer":
        mcp_args.extend(["--auth-token", bearer_token])

    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        mcp_proc = subprocess.Popen(
            mcp_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=creation_flags,
            preexec_fn=None if os.name == "nt" else os.setsid,
        )
    except Exception as e:
        return {"success": False, "error": f"启动 coding-tools-mcp 失败: {e}"}

    threading.Thread(target=stream_log, args=(mcp_proc.stdout, MCP_OUT_LOG, "MCP_OUT"), daemon=True).start()
    threading.Thread(target=stream_log, args=(mcp_proc.stderr, MCP_ERR_LOG, "MCP_ERR"), daemon=True).start()

    time.sleep(0.5)
    if mcp_proc.poll() is not None:
        return {"success": False, "error": "coding-tools-mcp 进程立即退出，请查看日志。"}

    if not wait_for_local_mcp(port):
        kill_pid_tree(mcp_proc.pid)
        return {"success": False, "error": f"本地 MCP 服务器未能在端口 {port} 上就绪。"}

    write_ui_log("Starting cloudflared tunnel services...")
    if mode == "named" and tunnel_name:
        if len(tunnel_name) > 60:
            tunnel_args = [cloudflared_exe, "tunnel", "run", "--token", tunnel_name]
        else:
            tunnel_args = [cloudflared_exe, "tunnel", "run", "--url", f"http://127.0.0.1:{port}", tunnel_name]
    else:
        tunnel_args = [
            cloudflared_exe, "tunnel",
            "--url", f"http://127.0.0.1:{port}",
            "--metrics", f"127.0.0.1:{metrics_port}",
        ]

    try:
        tunnel_proc = subprocess.Popen(
            tunnel_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
            preexec_fn=None if os.name == "nt" else os.setsid,
        )
    except Exception as e:
        kill_pid_tree(mcp_proc.pid)
        return {"success": False, "error": f"启动 cloudflared 失败: {e}"}

    threading.Thread(target=stream_log, args=(tunnel_proc.stdout, TUNNEL_OUT_LOG, "CF_OUT"), daemon=True).start()
    threading.Thread(target=stream_log, args=(tunnel_proc.stderr, TUNNEL_ERR_LOG, "CF_ERR"), daemon=True).start()

    tunnel_url = ""
    if permanent_domain:
        tunnel_url = f"https://{permanent_domain}"
        write_ui_log(f"Using configured permanent tunnel domain: {tunnel_url}")
    else:
        write_ui_log("Waiting for public Cloudflare tunnel URL...")
        deadline = time.time() + 40
        while time.time() < deadline:
            tunnel_url = get_tunnel_url_from_metrics(metrics_port)
            if not tunnel_url:
                tunnel_url = get_tunnel_url_from_log(wait_seconds=1)
            if tunnel_url:
                break
            time.sleep(0.5)
        if not tunnel_url:
            write_ui_log("Warning: cloudflared started but did not report a trycloudflare URL in time.")

    credential = password if auth_mode == "oauth" else (bearer_token if auth_mode == "bearer" else "")

    save_state(
        mode=mode,
        mcpPid=mcp_proc.pid,
        tunnelPid=tunnel_proc.pid,
        workspace=workspace,
        port=port,
        metricsPort=metrics_port,
        authMode=auth_mode,
        permissionMode=permission_mode,
        password=password,
        bearerToken=bearer_token,
        tunnelUrl=tunnel_url,
        tunnelName=tunnel_name,
        permanentDomain=permanent_domain,
    )

    write_ui_log(f"Start sequence complete. MCP PID: {mcp_proc.pid}, Tunnel PID: {tunnel_proc.pid}")
    if auth_mode == "noauth":
        write_ui_log("WARNING: 无认证模式已对外暴露隧道，仅建议本机调试使用！")

    tunnel_pid = tunnel_proc.pid
    if mode == "quick" and tunnel_url:
        def _keepalive(url, pid):
            while True:
                time.sleep(240)
                try:
                    if not test_process_alive(pid):
                        break
                    state_now = get_state()
                    if not state_now or state_now.get("tunnelUrl") != url:
                        break
                    urllib.request.urlopen(urllib.request.Request(url + "/mcp", headers={"Accept": "text/event-stream"}), timeout=10).read(1)
                except Exception:
                    pass
        threading.Thread(target=_keepalive, args=(tunnel_url, tunnel_pid), daemon=True).start()
        write_ui_log("Keepalive started (every 240s) to prevent QUIC idle timeout")

    return {"success": True, "tunnelUrl": tunnel_url, "credential": credential}


def stream_log(pipe, file_path, prefix):
    try:
        with open(file_path, "a", encoding="utf-8") as f:
            for line_bytes in iter(pipe.readline, b""):
                line = line_bytes.decode("utf-8", errors="replace")
                f.write(line)
                f.flush()
                stripped = line.strip()
                if stripped:
                    timestamp = time.strftime("%H:%M:%S")
                    with log_lock:
                        LOG_BUFFER.append(f"[{timestamp}] [{prefix}] {stripped}")
    except Exception as e:
        write_ui_log(f"Logger thread {prefix} exception: {e}")


def build_status_payload():
    state = get_state()
    default_port = 8765
    metrics_port = 20242

    eff_port = state.get("port", default_port) if state else default_port
    mcp_scan, tunnel_scan = get_managed_processes_by_port(eff_port)

    response_data = {
        "status": "stopped",
        "statusText": "STOPPED",
        "mode": "quick",
        "workspace": "",
        "port": default_port,
        "metricsPort": metrics_port,
        "authMode": "oauth",
        "permissionMode": "trusted",
        "tunnelName": "",
        "permanentDomain": "",
        "relayUrl": "",
        "tunnelUrl": "",
        "mcpUrl": "",
        "credential": "",
        "relay": None,
    }

    if state:
        mode = state.get("mode", "quick")
        response_data.update({
            "mode": mode,
            "workspace": state.get("workspace", ""),
            "port": state.get("port", default_port),
            "metricsPort": state.get("metricsPort", metrics_port),
            "authMode": state.get("authMode", "oauth"),
            "permissionMode": state.get("permissionMode", "trusted"),
            "tunnelName": state.get("tunnelName", ""),
            "permanentDomain": state.get("permanentDomain", ""),
            "relayUrl": state.get("relayUrl", ""),
        })

        if mode == "relay":
            relay_url = state.get("relayUrl", "")
            health = probe_relay(relay_url) if relay_url else {"ok": False, "detail": "no url"}
            normalized = normalize_relay_url(relay_url)
            mcp_url = ""
            if normalized:
                base, full = normalized
                mcp_url = full if full.endswith("/mcp") else base + "/mcp"
            response_data.update({
                "status": "relay-up" if health.get("ok") else "relay-down",
                "statusText": "RELAY UP" if health.get("ok") else "RELAY DOWN",
                "mcpUrl": mcp_url,
                "credential": "",
                "relay": health,
            })
            return response_data

        mcp_alive = test_process_alive(state.get("mcpPid"))
        tunnel_alive = test_process_alive(state.get("tunnelPid"))

        status_str = "stopped"
        status_text = "STOPPED"
        if mcp_alive and tunnel_alive:
            status_str, status_text = "running", "RUNNING"
        elif mcp_alive or tunnel_alive:
            status_str, status_text = "partial", "PARTIAL"

        metrics_p = state.get("metricsPort", metrics_port)
        url = get_tunnel_url_from_metrics(metrics_p) or state.get("tunnelUrl", "")

        credential = ""
        if state.get("authMode") == "oauth":
            credential = state.get("password", "") or get_password_from_log()
        elif state.get("authMode") == "bearer":
            credential = state.get("bearerToken", "")

        response_data.update({
            "status": status_str,
            "statusText": status_text,
            "tunnelUrl": url,
            "mcpUrl": f"{url}/mcp" if url else "",
            "credential": credential,
        })
        return response_data

    if mcp_scan and tunnel_scan:
        response_data["status"] = "running"
        response_data["statusText"] = "RUNNING (discovered)"
    elif mcp_scan or tunnel_scan:
        response_data["status"] = "partial"
        response_data["statusText"] = "PARTIAL (discovered)"

    pwd = get_password_from_log()
    url = get_tunnel_url_from_metrics(metrics_port) or get_tunnel_url_from_log()
    response_data.update({
        "credential": pwd,
        "tunnelUrl": url,
        "mcpUrl": f"{url}/mcp" if url else "",
        "workspace": state.get("workspace", "") if state else "",
    })
    return response_data


class GatewayRequestHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        url_path = urlparse(self.path).path

        if url_path == "/api/context/prompt":
            self.send_json({"prompt": BOOTSTRAP_PROMPT})
            return

        if url_path == "/" or url_path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(HTML_CONTENT.encode("utf-8"))

        elif url_path == "/api/status":
            self.send_json(build_status_payload())

        elif url_path == "/api/logs":
            with log_lock:
                current_logs = list(LOG_BUFFER)
            self.send_json({"logs": current_logs})

        else:
            self.send_error(404, "File not found")

    def do_POST(self):
        url_path = urlparse(self.path).path

        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length) if content_length > 0 else b""

        body_params = {}
        if post_data:
            try:
                body_params = json.loads(post_data.decode("utf-8"))
            except Exception:
                pass

        if url_path == "/api/browse":
            initial = body_params.get("initial_dir", os.getcwd())
            selected_path = browse_folder_dialog(initial)
            self.send_json({"path": selected_path or ""})

        elif url_path == "/api/start":
            mode = body_params.get("mode", "quick")
            if mode == "relay":
                result = start_relay_mode(body_params.get("relayUrl", ""))
                self.send_json(result)
                return
            workspace = body_params.get("workspace", "").strip()
            try:
                port = int(body_params.get("port", 8765))
                metrics_port = int(body_params.get("metricsPort", 20242))
            except ValueError:
                self.send_json({"success": False, "error": "端口必须是数字。"})
                return
            if not workspace:
                self.send_json({"success": False, "error": "工作区路径为空。"})
                return
            result = start_managed_services(
                mode=mode,
                workspace=workspace,
                port=port,
                metrics_port=metrics_port,
                auth_mode=body_params.get("authMode", "oauth"),
                permission_mode=body_params.get("permissionMode", "trusted"),
                tunnel_name=(body_params.get("tunnelName") or body_params.get("tunnel_name") or "").strip(),
                permanent_domain=(body_params.get("permanentDomain") or body_params.get("permanent_domain") or "").strip(),
            )
            self.send_json(result)

        elif url_path == "/api/stop":
            try:
                stop_managed_services()
                self.send_json({"success": True})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)})

        elif url_path == "/api/relay-check":
            relay_url = body_params.get("relayUrl", "")
            if not relay_url:
                self.send_json({"relay": {"ok": False, "detail": "empty url"}})
                return
            self.send_json({"relay": probe_relay(relay_url, force=True)})

        elif url_path == "/api/context/init":
            workspace = body_params.get("workspace", "").strip()
            if not workspace:
                self.send_json({"success": False, "error": "工作区路径为空。"})
                return
            self.send_json(init_session_relay(workspace))

        elif url_path == "/api/tunnel/auto-token":
            cert = os.path.join(os.path.expanduser("~"), ".cloudflared", "cert.pem")
            if not os.path.exists(cert):
                try:
                    subprocess.Popen(
                        ["powershell", "-NoProfile", "-Command", "Start-Process cloudflared -ArgumentList 'tunnel','login' -WindowStyle Normal"],
                    )
                except Exception:
                    pass
                self.send_json({"error": "浏览器未自动弹出？请手动打开 PowerShell 执行： cloudflared tunnel login  —  完成后再次点击「自动获取」"})
                return
            try:
                subprocess.run(["cloudflared", "tunnel", "create", "web-mcp-gateway"], capture_output=True, timeout=30)
                proc = subprocess.run(["cloudflared", "tunnel", "token", "web-mcp-gateway"], capture_output=True, timeout=15, text=True)
                token = (proc.stdout or proc.stderr or "").strip().split()[-1] if proc.stdout or proc.stderr else ""
                if len(token) < 60:
                    raise RuntimeError("token empty")
                self.send_json({"token": token})
            except Exception as e:
                self.send_json({"error": f"获取 Token 失败: {e}，请到 one.dash.cloudflare.com -> Networks -> Tunnels 手动复制"})

        elif url_path == "/api/context/prompt":
            self.send_json({"prompt": BOOTSTRAP_PROMPT})

        else:
            self.send_error(404, "Not Found")

    def send_json(self, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    def shutdown_handler(signum, frame):
        print("\nShutting down gateway background server...")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    if os.name != "nt":
        signal.signal(signal.SIGTERM, shutdown_handler)

    server_address = ("127.0.0.1", WEB_SERVER_PORT)
    socketserver.TCPServer.allow_reuse_address = True

    try:
        httpd = ThreadedHTTPServer(server_address, GatewayRequestHandler)
    except Exception as e:
        print(f"ERROR: Could not start gateway web server on port {WEB_SERVER_PORT}: {e}")
        print("请检查是否已有 Gateway 实例在运行。")
        sys.exit(1)

    print("==================================================================")
    print(f" {APP_NAME} Background Server Active")
    print(f" URL: http://127.0.0.1:{WEB_SERVER_PORT}")
    print("==================================================================")
    print("Opening Dashboard in your default web browser...")

    threading.Timer(0.5, lambda: webbrowser.open(f"http://127.0.0.1:{WEB_SERVER_PORT}")).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
