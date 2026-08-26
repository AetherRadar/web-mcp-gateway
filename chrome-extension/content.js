// 默认信任的 API 工具清单（当 storage 尚未初始化时的 fallback）
// 已对齐 coding-tools-mcp >= 0.3.0 的固定 18 工具目录
const DEFAULT_TRUSTED_TOOLS = new Set([
  "read_file",
  "list_dir",
  "list_files",
  "search_text",
  "apply_patch",
  "view_image",
  "exec_command",
  "write_stdin",
  "read_output",
  "kill_command",
  "request_permissions",
  "git_status",
  "git_diff",
  "git_log",
  "git_show",
  "git_blame",
  "server_info",
  "check_exec_environment"
]);

// 默认信任的 MCP 服务器清单
const DEFAULT_TRUSTED_SERVERS = new Set([
  "MCP Neverending Coding"
]);

let TRUSTED_TOOLS = new Set(DEFAULT_TRUSTED_TOOLS);
let TRUSTED_SERVERS = new Set(DEFAULT_TRUSTED_SERVERS);
let AUTO_APPROVE = false;

// 已经处理/点击过核准的 Dialog 元素，避免重复触发
const approvedDialogs = new WeakSet();

// 加载与监听设置变更
async function initConfig() {
  if (typeof chrome !== "undefined" && chrome.storage && chrome.storage.local) {
    try {
      const data = await chrome.storage.local.get(["autoApprove", "trustedTools", "trustedServers"]);
      if (data.autoApprove !== undefined) {
        AUTO_APPROVE = data.autoApprove;
      }
      if (data.trustedTools !== undefined) {
        // 与 DEFAULT_TRUSTED_TOOLS 并集，防止新追加的默认工具被排除
        TRUSTED_TOOLS = new Set([...DEFAULT_TRUSTED_TOOLS, ...data.trustedTools]);
      }
      if (data.trustedServers !== undefined) {
        // 与 DEFAULT_TRUSTED_SERVERS 并集，防止新追加的默认服务器被排除
        TRUSTED_SERVERS = new Set([...DEFAULT_TRUSTED_SERVERS, ...data.trustedServers]);
      }
    } catch (e) {
      console.error("无法自 chrome.storage 加载设置，使用默认值:", e);
    }

    // 监听来自 options 页面的实时变更
    chrome.storage.onChanged.addListener((changes, namespace) => {
      if (namespace === "local") {
        if (changes.autoApprove) {
          AUTO_APPROVE = changes.autoApprove.newValue;
        }
        if (changes.trustedTools) {
          TRUSTED_TOOLS = new Set(changes.trustedTools.newValue);
        }
        if (changes.trustedServers) {
          TRUSTED_SERVERS = new Set(changes.trustedServers.newValue);
        }
        // 设置变更后立即重新扫描
        scan();
      }
    });
  }
}

// 动态新增信任工具并储存
async function addNewTrustedTool(toolName) {
  if (!toolName) return;
  TRUSTED_TOOLS.add(toolName);

  if (typeof chrome !== "undefined" && chrome.storage && chrome.storage.local) {
    try {
      await chrome.storage.local.set({
        trustedTools: Array.from(TRUSTED_TOOLS)
      });
      console.log(`[MCP Helper] 已将工具 "${toolName}" 加入信任清单`);
    } catch (e) {
      console.error("无法将新工具存入 chrome.storage:", e);
    }
  } else {
    // Fallback 本地存储（为 test_page 本地测试提供支持）
    try {
      const fallbackSettings = JSON.parse(localStorage.getItem("mcp_approval_settings_fallback_v2") || '{"autoApprove":false,"trustedTools":[],"trustedServers":[]}');
      if (!fallbackSettings.trustedTools.includes(toolName)) {
        fallbackSettings.trustedTools.push(toolName);
        localStorage.setItem("mcp_approval_settings_fallback_v2", JSON.stringify(fallbackSettings));
      }
      scan();
    } catch (e) {
      console.error("无法写入 fallback 存储:", e);
    }
  }
}

// 动态新增信任服务器并储存
async function addNewTrustedServer(serverName) {
  if (!serverName) return;
  TRUSTED_SERVERS.add(serverName);

  if (typeof chrome !== "undefined" && chrome.storage && chrome.storage.local) {
    try {
      await chrome.storage.local.set({
        trustedServers: Array.from(TRUSTED_SERVERS)
      });
      console.log(`[MCP Helper] 已将服务器 "${serverName}" 加入信任清单`);
    } catch (e) {
      console.error("无法将新服务器存入 chrome.storage:", e);
    }
  } else {
    // Fallback 本地存储
    try {
      const fallbackSettings = JSON.parse(localStorage.getItem("mcp_approval_settings_fallback_v2") || '{"autoApprove":false,"trustedTools":[],"trustedServers":[]}');
      if (!fallbackSettings.trustedServers.includes(serverName)) {
        fallbackSettings.trustedServers.push(serverName);
        localStorage.setItem("mcp_approval_settings_fallback_v2", JSON.stringify(fallbackSettings));
      }
      scan();
    } catch (e) {
      console.error("无法写入 fallback 存储:", e);
    }
  }
}

function getVisibleText(el) {
  return (el?.innerText || el?.textContent || "").trim();
}

// 寻找 MCP 授权 dialog（兼容 ChatGPT / Claude / Grok 的弹窗结构）
function findApprovalDialog() {
  // 方法 1：寻找标准 role="dialog" 或 aria-modal="true"
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"]'));
  for (const el of dialogs) {
    const text = getVisibleText(el);
    const hasAllow = text.includes("Allow") || text.includes("允许") || text.includes("同意") ||
                     text.includes("Approve") || text.includes("批准") || text.includes("授权");
    const hasMcp = text.includes("MCP") || text.includes("tool") || text.includes("工具") ||
                   text.includes("connector") || text.includes("连接器");
    if (hasAllow && hasMcp) {
      return el;
    }
  }

  // 方法 2：反向寻找法（针对非标准 dialog 结构编排且包含允许按钮与 MCP 关键字的情境）
  const buttons = Array.from(document.querySelectorAll("button"));
  for (const btn of buttons) {
    if (btn.offsetWidth === 0 && btn.offsetHeight === 0) continue; // 排除隐藏按钮

    const btnText = getVisibleText(btn).toLowerCase();
    const isAllowBtn = btnText === "allow" || btnText.includes("allow") || btnText.includes("允许") ||
                       btnText.includes("同意") || btnText.includes("approve") ||
                       btnText.includes("批准") || btnText.includes("授权");

    if (isAllowBtn) {
      // 往上寻找最近的容器，并检查容器内是否包含 MCP 相关关键字
      let parent = btn.parentElement;
      let depth = 0;
      // 往上找最多 6 层
      while (parent && depth < 6) {
        const parentText = getVisibleText(parent);
        const hasMcp = parentText.includes("MCP") || parentText.includes("tool") || parentText.includes("工具") ||
                       parentText.includes("connector") || parentText.includes("连接器");
        if (hasMcp) {
          return parent;
        }
        parent = parent.parentElement;
        depth++;
      }
    }
  }

  return null;
}

// 从 dialog 文字中抽取 tool/server 名称
function extractToolName(dialog) {
  const text = getVisibleText(dialog);

  // 1. 优先以完整匹配 allowlist 中的名称（最精准）
  for (const server of TRUSTED_SERVERS) {
    if (text.includes(server)) return { name: server, type: "server" };
  }
  for (const tool of TRUSTED_TOOLS) {
    if (text.includes(tool)) return { name: tool, type: "tool" };
  }

  // 2. 针对 ChatGPT 特定问句的正则表达式提取 MCP Server 名称
  // 中文："要允许 ChatGPT 使用 MCP Neverending Coding 吗？"
  const zhMatch = text.match(/使用\s*([a-zA-Z0-9_\s\-]+)\s*吗/);
  if (zhMatch && zhMatch[1]) {
    const extracted = zhMatch[1].trim();
    if (extracted.toLowerCase().includes("mcp") || extracted.length > 3) {
      return { name: extracted, type: "server" };
    }
  }

  // 英文："Allow ChatGPT to use MCP Neverending Coding?"
  const enMatch = text.match(/use\s*([a-zA-Z0-9_\s\-]+)\?/i);
  if (enMatch && enMatch[1]) {
    const extracted = enMatch[1].trim();
    if (extracted.toLowerCase().includes("mcp") || extracted.length > 3) {
      return { name: extracted, type: "server" };
    }
  }

  // 3. 模糊匹配：寻找底线连接的标记，如 "exec_command" 等
  const match = text.match(/[a-zA-Z_][a-zA-Z0-9_]{2,}/g);
  if (!match) return { name: null, type: "unknown" };

  const foundToolName = match.find((token) =>
    token.includes("_") &&
    !["read_only", "tool_call", "tool_calls"].includes(token.toLowerCase())
  ) || null;

  return { name: foundToolName, type: foundToolName ? "tool" : "unknown" };
}

// 寻找 Allow / Approve 按钮
function findAllowButton(dialog) {
  const buttons = Array.from(dialog.querySelectorAll("button"));

  // 优先级 1：寻找包含 "always allow" 或 "一律允许" 的按钮，以达到最大 YOLO 效果
  const alwaysAllowBtn = buttons.find(button => {
    const text = getVisibleText(button).toLowerCase();
    return text.includes("always allow") || text.includes("一律允许") || text.includes("一律同意") ||
           text.includes("always approve") || text.includes("总是允许");
  });
  if (alwaysAllowBtn) return alwaysAllowBtn;

  // 优先级 2：寻找一般的 allow / approve 按钮
  return buttons.find((button) => {
    const text = getVisibleText(button).toLowerCase();
    return (
      text === "allow" ||
      text.includes("allow") ||
      text.includes("允许") ||
      text.includes("同意") ||
      text.includes("approve") ||
      text.includes("批准") ||
      text.includes("授权")
    );
  }) || null;
}

// 显示或更新右上角状态 Badge
function showBadge(dialog, name, type, trusted) {
  let badge = document.getElementById("mcp-approval-helper-badge");

  if (!badge) {
    badge = document.createElement("div");
    badge.id = "mcp-approval-helper-badge";
    badge.style.position = "fixed";
    badge.style.zIndex = "2147483647";
    badge.style.top = "20px";
    badge.style.right = "20px";
    badge.style.padding = "12px 16px";
    badge.style.borderRadius = "12px";
    badge.style.fontSize = "13px";
    badge.style.fontWeight = "600";
    badge.style.fontFamily = "system-ui, -apple-system, BlinkMacSystemFont, sans-serif";
    badge.style.boxShadow = "0 8px 30px rgba(0, 0, 0, 0.3)";
    badge.style.transition = "all 0.3s ease";
    badge.style.opacity = "0";
    badge.style.transform = "translateY(-10px)";
    document.body.appendChild(badge);

    // 微动画滑入
    requestAnimationFrame(() => {
      badge.style.opacity = "1";
      badge.style.transform = "translateY(0)";
    });
  }

  const typeText = type === "server" ? "服务器" : "工具";

  // 设置 Badge 样式与文字
  if (trusted) {
    badge.style.background = "rgba(16, 185, 129, 0.95)"; /* Emerald Green */
    badge.style.border = "1px solid rgba(255, 255, 255, 0.1)";
    badge.style.color = "white";
    badge.innerHTML = `🛡️ 信任的 MCP ${typeText}: <span style="text-decoration: underline;">${name || "未知"}</span><br><span style="font-size: 11px; font-weight: normal; opacity: 0.9;">按 Ctrl+Shift+Y 或点击页面核准</span>`;
    dialog.style.outline = "4px solid #10b981";
    dialog.style.outlineOffset = "2px";
  } else {
    badge.style.background = "rgba(239, 68, 68, 0.95)"; /* Rose Red */
    badge.style.border = "1px solid rgba(255, 255, 255, 0.1)";
    badge.style.color = "white";

    // 一键信任按钮
    let buttonHtml = "";
    if (name) {
      const btnText = type === "server" ? "一律信任此服务器" : "一律信任此工具";
      buttonHtml = `<br><button id="mcp-helper-trust-btn" style="margin-top: 8px; display: inline-block; background: white; color: #dc3545; border: none; padding: 5px 12px; border-radius: 6px; font-weight: 700; cursor: pointer; font-size: 11px; font-family: inherit; box-shadow: 0 2px 5px rgba(0,0,0,0.2); transition: all 0.2s;">${btnText}</button>`;
    }

    badge.innerHTML = `⚠️ 未信任的 MCP ${typeText}: <span style="text-decoration: underline;">${name || "未知"}</span><br><span style="font-size: 11px; font-weight: normal; opacity: 0.9;">需手动点击或按 Ctrl+Shift+Y 二次确认</span>${buttonHtml}`;
    dialog.style.outline = "4px solid #ef4444";
    dialog.style.outlineOffset = "2px";

    // 绑定点击事件
    if (name) {
      setTimeout(() => {
        const trustBtn = document.getElementById("mcp-helper-trust-btn");
        if (trustBtn) {
          trustBtn.onclick = async () => {
            if (type === "server") {
              await addNewTrustedServer(name);
            } else {
              await addNewTrustedTool(name);
            }
          };
        }
      }, 0);
    }
  }
}

// 移除 Badge 与 Dialog 的样式
function removeBadgeAndOutline() {
  const badge = document.getElementById("mcp-approval-helper-badge");
  if (badge) {
    badge.style.opacity = "0";
    badge.style.transform = "translateY(-10px)";
    setTimeout(() => badge.remove(), 300);
  }

  // 清除页面上可能的 outline 样式
  const dialogs = document.querySelectorAll('[role="dialog"], div');
  dialogs.forEach(el => {
    if (el.style.outline) {
      el.style.outline = "";
      el.style.outlineOffset = "";
    }
  });
}

// 记录审计日志至 localStorage
function logApproval(toolName, trusted) {
  const key = "mcpApprovalHelperLog";
  try {
    const oldLog = JSON.parse(localStorage.getItem(key) || "[]");
    oldLog.push({
      toolName,
      trusted,
      time: new Date().toISOString(),
      url: location.href
    });
    // 保留最后 200 笔
    localStorage.setItem(key, JSON.stringify(oldLog.slice(-200)));
  } catch (e) {
    console.error("无法写入审计日志:", e);
  }
}

// 模拟真实的鼠标完整点击事件流，提升 React 绑定与防护绕过的兼容性
function simulateRealClick(element) {
  if (!element) return;
  const events = ["pointerdown", "mousedown", "pointerup", "mouseup", "click"];
  events.forEach(eventName => {
    const event = new MouseEvent(eventName, {
      bubbles: true,
      cancelable: true,
      view: window,
      isTrusted: true
    });
    element.dispatchEvent(event);
  });
}

// 检查按钮目前是否已经"开放点击"
function isButtonEnabled(button) {
  if (!button) return false;

  // 1. 检查原生 disabled 属性
  if (button.disabled) return false;

  // 2. 检查 aria-disabled 状态
  if (button.getAttribute("aria-disabled") === "true") return false;

  // 3. 检查 class 是否有隐性 disabled 或 loading 特征 (防范客制化按钮)
  const classList = Array.from(button.classList).map(c => c.toLowerCase());
  const isBlocked = classList.some(c => c.includes("disabled") || c.includes("loading") || c.includes("wait"));
  if (isBlocked) return false;

  return true;
}

// 进行温和的多阶段重试点击，解决 React 异步事件注册延迟，同时避免高频机械化行为被检测
function autoApproveWithRetry(dialog, toolName) {
  // 不使用高频的 setInterval 点击（避免被 Cloudflare/OpenAI 检测为 Bot 操作）
  // 改用模拟人类正常反应的"温和且递增的 30 秒时间序列 (30s Humanized Delay Sequence)"
  const delaySequence = [
    100, 400, 800, 1500, 2500, 4000, 6000, 9000, 12000, 16000, 20000, 25000, 30000
  ]; // 30秒内共尝试点击 13 次，间隔逐渐拉长

  delaySequence.forEach((delay, index) => {
    setTimeout(() => {
      // 点击成功关闭后：如果 dialog 已经在 DOM 中被关闭，直接停止后续所有的点击！
      if (!document.body.contains(dialog)) return;

      const allowButton = findAllowButton(dialog);
      // 关键优化：只有检测到按钮"已开放点击 (Enabled)"时，才发送真实点击模拟！
      if (allowButton && isButtonEnabled(allowButton)) {
        console.log(`[MCP Helper] 检测到按钮开放点击，执行核准 (第 ${index + 1} 次, 延迟: ${delay}ms)`);
        simulateRealClick(allowButton);
      } else if (allowButton) {
        console.log(`[MCP Helper] 发现按钮，但目前处于停用状态，跳过此点击 (延迟: ${delay}ms)`);
      }
    }, delay);
  });
}

// 扫描页面并处理核准逻辑
function scan() {
  const dialog = findApprovalDialog();

  if (!dialog) {
    removeBadgeAndOutline();
    return;
  }

  // 避免重复处理已核准的同一个弹窗
  if (approvedDialogs.has(dialog)) return;

  const { name, type } = extractToolName(dialog);
  const trusted = name && (
    (type === "server" && TRUSTED_SERVERS.has(name)) ||
    (type === "tool" && TRUSTED_TOOLS.has(name))
  );

  showBadge(dialog, name, type, trusted);

  // 自动核准逻辑：如果启用自动核准且该工具为信任工具
  if (AUTO_APPROVE && trusted) {
    approvedDialogs.add(dialog);
    logApproval(name, true);
    console.log(`[MCP Helper] 自动核准信任${type === "server" ? "服务器" : "工具"}: ${name}`);

    // 执行温和重试点击机制
    autoApproveWithRetry(dialog, name);
  }
}

// 快捷键监听器 Ctrl + Shift + Y
document.addEventListener("keydown", (event) => {
  if (!(event.ctrlKey && event.shiftKey && event.key.toLowerCase() === "y")) {
    return;
  }

  const dialog = findApprovalDialog();
  if (!dialog) return;

  if (approvedDialogs.has(dialog)) return;

  const { name, type } = extractToolName(dialog);
  const trusted = name && (
    (type === "server" && TRUSTED_SERVERS.has(name)) ||
    (type === "tool" && TRUSTED_TOOLS.has(name))
  );

  // 若非信任工具，跳出 confirm 询问二次确认而非直接封锁
  if (!trusted) {
    const typeText = type === "server" ? "服务器" : "工具";
    const confirmApprove = confirm(`警告：此 MCP ${typeText} [${name || "未知"}] 不在您的信任清单中。\n\n您确定要手动核准并执行吗？`);
    if (!confirmApprove) return;
  }

  const allowButton = findAllowButton(dialog);
  if (!allowButton) {
    alert("找不到"允许"或"Allow"按钮，请手动点击。");
    return;
  }

  approvedDialogs.add(dialog);
  logApproval(name, trusted);
  console.log(`[MCP Helper] 快捷键核准${type === "server" ? "服务器" : "工具"}: ${name} (信任状态: ${trusted})`);
  simulateRealClick(allowButton);
});

let scanPending = false;
function throttleScan() {
  if (scanPending) return;
  scanPending = true;
  requestAnimationFrame(() => {
    scan();
    scanPending = false;
  });
}

// 使用 MutationObserver 监控 DOM
const observer = new MutationObserver(throttleScan);
observer.observe(document.documentElement, {
  childList: true,
  subtree: true
});

// 初始化设置并执行首次扫描
initConfig().then(() => {
  throttleScan();
});
