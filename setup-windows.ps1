# web-mcp-gateway Windows 环境一键安装
# 用法: powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1

$ErrorActionPreference = "Continue"

function Test-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

Write-Host "==> [1/4] 检查 Python (>= 3.11)..." -ForegroundColor Cyan
$needPython = $true
if (Test-Command python) {
    $ver = (python --version 2>$null)
    if ($ver -match "Python (\d+)\.(\d+)") {
        $major = [int]$Matches[1]; $minor = [int]$Matches[2]
        if (($major -gt 3) -or ($major -eq 3 -and $minor -ge 11)) {
            Write-Host "    已安装 $ver，跳过" -ForegroundColor Green
            $needPython = $false
        }
    }
}
if ($needPython) {
    Write-Host "    未检测到可用的 Python，通过 winget 安装 Python 3.12..." -ForegroundColor Yellow
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    winget 安装失败，请手动安装: https://www.python.org/downloads/" -ForegroundColor Red
        Write-Host "    安装时务必勾选 'Add python.exe to PATH'" -ForegroundColor Red
        exit 1
    }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
}

Write-Host "==> [2/4] 检查 cloudflared..." -ForegroundColor Cyan
if (-not (Test-Command cloudflared)) {
    $localBin = Join-Path $env:USERPROFILE ".local\bin"
    if (Test-Path (Join-Path $localBin "cloudflared.exe")) {
        Write-Host "    已存在于 $localBin（未加入 PATH 时由 Gateway 自动发现）" -ForegroundColor Green
    } else {
        Write-Host "    通过 winget 安装 Cloudflare cloudflared..." -ForegroundColor Yellow
        winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -ne 0) {
            Write-Host "    winget 安装失败，尝试直接下载到 $localBin ..." -ForegroundColor Yellow
            New-Item -ItemType Directory -Force -Path $localBin | Out-Null
            Invoke-WebRequest -UseBasicParsing `
                -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
                -OutFile (Join-Path $localBin "cloudflared.exe")
        }
    }
} else {
    Write-Host "    已安装: $(Get-Command cloudflared).Source" -ForegroundColor Green
}

Write-Host "==> [3/4] 安装 coding-tools-mcp (PyPI)..." -ForegroundColor Cyan
$py = if (Test-Command python) { "python" } else { "py" }
& $py -m pip install --user --upgrade coding-tools-mcp
if ($LASTEXITCODE -ne 0) {
    Write-Host "    pip 安装失败，请检查网络/代理后重试" -ForegroundColor Red
    exit 1
}

Write-Host "==> [4/4] 验证安装..." -ForegroundColor Cyan
& $py -m pip show coding-tools-mcp | Select-String "Name|Version|Location"
Write-Host ""
Write-Host "安装完成！启动 Gateway:" -ForegroundColor Green
Write-Host "    python .\gateway.py"
Write-Host "浏览器会自动打开 http://127.0.0.1:8766 控制台"
