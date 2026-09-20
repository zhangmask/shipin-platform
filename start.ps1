# P6：本地一键启动（Windows，无 Docker 时）—— 构建前端 → 起 API（8766）
# 用法：.\start.ps1 [-Port 8766] [-Reload]
param(
    [int]$Port = 8766,
    [switch]$Reload
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "[start] 检查/构建前端（web/dist）…"
if (-not (Test-Path "web/node_modules")) {
    Write-Host "[start] 首次运行，安装前端依赖（npm ci）…"
    Push-Location web
    npm ci --no-audit --no-fund
    Pop-Location
}
Push-Location web
npm run build | Out-Null
Pop-Location

$env:PYTHONPATH = "$PWD\src"
$args = @("src.api:app", "--host", "127.0.0.1", "--port", "$Port")
if ($Reload) { $args += "--reload" }
Write-Host "[start] uvicorn http://127.0.0.1:$Port （/ui 面板 | /api 接口）"
Write-Host "[start] 鉴权模式: SHIPIN_AUTH_MODE=$env:SHIPIN_AUTH_MODE"
python -m uvicorn @args