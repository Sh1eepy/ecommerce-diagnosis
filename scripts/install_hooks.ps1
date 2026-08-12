# 安装 git hooks（把 hooks/ 里的脚本复制到 .git/hooks/）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\install_hooks.ps1
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$src = Join-Path $repo "hooks"
$dst = Join-Path $repo ".git\hooks"
if (-not (Test-Path $dst)) { Write-Host ".git/hooks 不存在，请确认这是 git 仓库"; exit 1 }
Get-ChildItem $src -File | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $dst $_.Name) -Force
    Write-Host ("installed: " + $_.Name)
}
Write-Host "hooks 安装完成"
