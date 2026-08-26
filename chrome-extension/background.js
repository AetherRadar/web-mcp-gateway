chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "GET_MCP_URL") {
    fetch("http://127.0.0.1:8766/api/status", { cache: "no-store" })
      .then(r => r.json())
      .then(j => {
        const url = j.mcpUrl || (j.tunnelUrl ? j.tunnelUrl + "/mcp" : "");
        sendResponse({ url });
      })
      .catch(() => sendResponse({ url: null }));
    return true;
  }
});
