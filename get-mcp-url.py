#!/usr/bin/env python3
"""Auto-fetch MCP URL from local gateway — for Grok/ChatGPT/any client.

Usage:
  python get-mcp-url.py              # print URL
  python get-mcp-url.py --copy       # print + copy to clipboard (Windows)
  python get-mcp-url.py --open-grok  # open Grok Custom Connector page
"""
import sys, json, urllib.request, webbrowser, argparse, subprocess, shutil

GATEWAY = "http://127.0.0.1:8766/api/status"

def fetch():
    with urllib.request.urlopen(GATEWAY, timeout=5) as r:
        j = json.load(r)
    url = j.get("mcpUrl") or (j.get("tunnelUrl") + "/mcp" if j.get("tunnelUrl") else "")
    return url, j

def copy_clip(text):
    if sys.platform == "win32":
        try:
            # Use powershell Set-Clipboard (works without extra deps)
            subprocess.run(["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Value '{text}'"], check=False, timeout=5)
            return True
        except: pass
    if shutil.which("xclip"):
        subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=False, timeout=5)
        return True
    if shutil.which("pbcopy"):
        subprocess.run(["pbcopy"], input=text.encode(), check=False, timeout=5)
        return True
    return False

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--copy", action="store_true", help="copy to clipboard")
    p.add_argument("--open-grok", action="store_true", help="open grok.com")
    args = p.parse_args()
    try:
        url, j = fetch()
    except Exception as e:
        print(f"[-] Gateway not running? {GATEWAY} -> {e}", file=sys.stderr)
        print("    Run: python gateway.py", file=sys.stderr)
        sys.exit(1)
    if not url or "trycloudflare" not in url and "mcp" not in url:
        print(f"[-] No tunnel URL yet. Status: {j.get('status')} — start gateway first.", file=sys.stderr)
        sys.exit(1)
    print(url)
    if args.copy:
        if copy_clip(url): print("[+] Copied to clipboard")
        else: print("[!] Copy failed — please copy manually")
    if args.open_grok:
        webbrowser.open("https://grok.com")
        print("[*] Opened grok.com — Custom Connector dialog will auto-fill via extension")
