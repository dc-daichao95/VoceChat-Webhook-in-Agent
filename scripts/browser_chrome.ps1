<#
.SYNOPSIS
    以 CDP 远程调试端口启动专用 Chrome,供 browser-use 连接。
.DESCRIPTION
    使用独立的用户数据目录(.browser-use-profile),不复用日常登录态,避免误操作已登录账号。
.EXAMPLE
    powershell -File scripts/browser_chrome.ps1
    powershell -File scripts/browser_chrome.ps1 -Port 9222
#>
param(
    [int]$Port = 9222
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chrome)) {
    throw "未找到 Chrome:$chrome"
}
$profileDir = Join-Path $repo ".browser-use-profile"
Start-Process $chrome -ArgumentList @("--remote-debugging-port=$Port", "--user-data-dir=$profileDir")
Write-Output "[OK] Chrome 已启动,CDP: http://127.0.0.1:$Port  profile: $profileDir"
