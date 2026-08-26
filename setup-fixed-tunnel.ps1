# 一键获取 Cloudflare 固定隧道 Token（需有已托管到 Cloudflare 的域名）
# 用法: powershell -ExecutionPolicy Bypass -File .\setup-fixed-tunnel.ps1
# 首次会弹浏览器让你登录 Cloudflare，按提示选账号即可

$TunnelName = "web-mcp-gateway"
$Cert = "$env:USERPROFILE\.cloudflared\cert.pem"

if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
  Write-Host "[-] 未找到 cloudflared，请先运行 setup-windows.ps1" -ForegroundColor Red; exit 1
}

if (-not (Test-Path $Cert)) {
  Write-Host "[*] 首次使用，需要登录 Cloudflare（会弹浏览器）..." -ForegroundColor Cyan
  cloudflared tunnel login
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Cert)) { Write-Host "[-] 登录失败，请重试" -ForegroundColor Red; exit 1 }
  Write-Host "[+] 登录成功" -ForegroundColor Green
}

Write-Host "[*] 创建隧道 $TunnelName ..." -ForegroundColor Cyan
cloudflared tunnel create $TunnelName 2>&1 | Out-String | Write-Host
# 已存在也无妨，继续取 token

Write-Host "[*] 获取 Token ..." -ForegroundColor Cyan
$token = cloudflared tunnel token $TunnelName 2>$null
if (-not $token) {
  # 旧版 cloudflared 没 token 子命令，尝试从 config 读取
  $token = cloudflared tunnel token $TunnelName 2>&1 | Select-Object -Last 1
}
if ($token -and $token.Length -gt 60) {
  Write-Host "`n[+] Token 已获取（已复制到剪贴板，请粘贴到 Gateway 的「固定域名隧道」里）：" -ForegroundColor Green
  Write-Host $token -ForegroundColor Yellow
  try { Set-Clipboard -Value $token; Write-Host "[+] 已复制到剪贴板" -ForegroundColor Green } catch {}
  Write-Host "`n下一步：去 Gateway 选「固定域名隧道」粘贴 Token，再填你的永久域名（如 mcp.example.com）并到 Cloudflare DNS 添加 CNAME -> <tunnel-id>.cfargotunnel.com" -ForegroundColor Cyan
} else {
  Write-Host "[-] 未取到 Token，请手动到 https://one.dash.cloudflare.com -> Networks -> Tunnels 复制" -ForegroundColor Yellow
}
