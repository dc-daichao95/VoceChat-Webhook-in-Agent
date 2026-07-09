<#
.SYNOPSIS
    打包 receiver 的最小构建上下文,便于传到 fnOS NAS 上构建镜像。

.DESCRIPTION
    只收集 receiver 运行所需文件(Dockerfile、requirements.txt、docker-compose.yml、app/),
    排除 __pycache__,输出一个 .tar.gz。把它上传到 NAS 解压后:
        docker compose up -d --build

.EXAMPLE
    powershell -File scripts/package_receiver.ps1
    powershell -File scripts/package_receiver.ps1 -Output build/receiver.tar.gz
#>
param(
    [string]$Output = "build/receiver-build-context.tar.gz"
)

$ErrorActionPreference = "Stop"

# 切到仓库根(脚本位于 scripts/ 下)
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$items = @("Dockerfile", "requirements.txt", "docker-compose.yml", "app")
foreach ($i in $items) {
    if (-not (Test-Path $i)) {
        throw "缺少构建上下文项:$i(请在仓库根运行)"
    }
}

# 确保输出目录存在
$outDir = Split-Path -Parent $Output
if ($outDir -and -not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
}

# 用系统自带 tar(Windows 10/Server 2019+ 内置 bsdtar)打包,排除字节码缓存
tar -czf $Output --exclude="__pycache__" @items
if ($LASTEXITCODE -ne 0) {
    throw "tar 打包失败(exit $LASTEXITCODE)"
}

$size = "{0:N1} KB" -f ((Get-Item $Output).Length / 1KB)
Write-Output "[OK] 已生成:$Output ($size)"
Write-Output "--- 包含文件 ---"
tar -tzf $Output
Write-Output ""
Write-Output "下一步:上传 $Output 到 NAS,解压后在该目录执行:"
Write-Output "    docker compose up -d --build"
