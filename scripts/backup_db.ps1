# 数据库备份脚本：mysqldump 导出 + 保留最近 N 份
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\backup_db.ps1          # 备份（保留 7 份）
#   powershell -ExecutionPolicy Bypass -File scripts\backup_db.ps1 -Keep 14 # 保留 14 份
#
# 说明：
#   - 配置从项目根 .env 读取（DB_HOST/DB_PORT/DB_NAME/DB_WRITE_USER/DB_WRITE_PASSWORD）
#   - 密码通过 MYSQL_PWD 环境变量传递，不出现在命令行（避免被进程列表看到）
#   - 备份文件在 backups\ 目录，超过 Keep 份自动删除最旧的
#
# 想每天自动备份：任务计划程序 → 新建任务 → 触发器"每天" → 操作里填上面这条命令

param([int]$Keep = 7)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root ".env"

if (-not (Test-Path $envFile)) {
    Write-Host "[ERR] 找不到 .env：$envFile" -ForegroundColor Red
    exit 1
}

# 解析 .env 中的 DB_* 配置（兼容 "KEY=value" 与 "KEY = value"）
$db = @{}
Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*DB_([A-Z_]+)\s*=\s*(.*?)\s*$') {
        $db[$matches[1]] = $matches[2]
    }
}

foreach ($k in @("HOST", "PORT", "NAME", "WRITE_USER", "WRITE_PASSWORD")) {
    if (-not $db.ContainsKey($k)) {
        Write-Host "[ERR] .env 缺少 DB_$k 配置" -ForegroundColor Red
        exit 1
    }
}

# mysqldump 可执行文件：优先 MySQL 8.0 安装路径（与项目数据库版本匹配），否则用 PATH 中的
$mysqldump = @(
    "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqldump.exe",
    "C:\Program Files (x86)\MySQL\MySQL Server 8.0\bin\mysqldump.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $mysqldump) {
    $g = Get-Command mysqldump -ErrorAction SilentlyContinue
    if ($g) { $mysqldump = $g.Source }
}
if (-not $mysqldump) {
    Write-Host "[ERR] 找不到 mysqldump，请确认 MySQL 的 bin 目录已加入 PATH" -ForegroundColor Red
    exit 1
}

$backupDir = Join-Path $root "backups"
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$outFile = Join-Path $backupDir "backup_$stamp.sql"

Write-Host "备份中: $($db['NAME']) @ $($db['HOST']):$($db['PORT']) -> $outFile"
$env:MYSQL_PWD = $db["WRITE_PASSWORD"]   # 密码只进环境变量，不进命令行
try {
    & $mysqldump -h $db["HOST"] -P $db["PORT"] -u $db["WRITE_USER"] $db["NAME"] | Out-File -FilePath $outFile -Encoding utf8
    Remove-Item Env:\MYSQL_PWD
} catch {
    Remove-Item Env:\MYSQL_PWD -ErrorAction SilentlyContinue
    Write-Host "[ERR] 备份失败: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

$size = (Get-Item $outFile).Length
Write-Host "完成: $([math]::Round($size / 1MB, 2)) MB"

# 清理旧备份：只保留最近 Keep 份
$old = Get-ChildItem $backupDir -Filter "backup_*.sql" | Sort-Object Name -Descending | Select-Object -Skip $Keep
foreach ($f in $old) {
    Remove-Item $f.FullName -Force
    Write-Host "清理旧备份: $($f.Name)"
}
Write-Host "当前备份数: $((Get-ChildItem $backupDir -Filter 'backup_*.sql').Count) / 上限 $Keep"
