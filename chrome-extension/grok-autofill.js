// Auto-fill Grok Custom Connector dialog from local gateway
// Fetches http://127.0.0.1:8766/api/status and fills Name + Server URL
const GATEWAY = "http://127.0.0.1:8766";

async function fetchMcpUrl() {
  try {
    const r = await fetch(`${GATEWAY}/api/status`, { cache: "no-store" });
    if (!r.ok) return null;
    const j = await r.json();
    return j.mcpUrl || j.tunnelUrl ? (j.mcpUrl || j.tunnelUrl + "/mcp") : null;
  } catch { return null; }
}

function fillGrokDialog(mcpUrl) {
  if (!mcpUrl || !mcpUrl.includes("http")) return;
  // Grok dialog: two inputs, placeholders "My Connector" and "https://mcp.example.com/sse"
  const inputs = document.querySelectorAll('input[placeholder*="mcp.example.com"], input[placeholder="My Connector"]');
  // More robust: find dialog with "Custom Connector" title
  const dialog = Array.from(document.querySelectorAll('[role="dialog"], div')).find(el => el.textContent.includes("Custom Connector"));
  const scope = dialog || document;
  const allInputs = scope.querySelectorAll("input");
  if (allInputs.length < 2) return;
  // First = Name, Second = Server URL (as per screenshot)
  const nameInput = allInputs[0];
  const urlInput = allInputs[1];
  if (urlInput && (!urlInput.value || urlInput.value.includes("mcp.example.com"))) {
    urlInput.focus();
    document.execCommand("selectAll", false, null);
    // Use native value setter to trigger React
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(urlInput), "value")?.set || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    if (setter) setter.call(urlInput, mcpUrl);
    else urlInput.value = mcpUrl;
    urlInput.dispatchEvent(new Event("input", { bubbles: true }));
    urlInput.dispatchEvent(new Event("change", { bubbles: true }));
    urlInput.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true }));
  }
  if (nameInput && !nameInput.value) {
    const setter2 = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(nameInput), "value")?.set || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    if (setter2) setter2.call(nameInput, "web-mcp-gateway");
    else nameInput.value = "web-mcp-gateway";
    nameInput.dispatchEvent(new Event("input", { bubbles: true }));
  }
}

let grokFilled = false;
async function tryAutofill() {
  if (grokFilled) return;
  const hasDialog = document.body.textContent.includes("Custom Connector") && document.body.textContent.includes("Server URL");
  if (!hasDialog) return;
  const url = await fetchMcpUrl();
  if (url) {
    fillGrokDialog(url);
    grokFilled = true;
    setTimeout(() => { grokFilled = false; }, 5000);
  }
}

const grokObserver = new MutationObserver(tryAutofill);
grokObserver.observe(document.documentElement, { childList: true, subtree: true });
setInterval(tryAutofill, 1500);
