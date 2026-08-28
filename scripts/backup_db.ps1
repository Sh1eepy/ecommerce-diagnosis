# Publish .sql only after a successful export. Use -NoPrune before migrations.
param(
    [ValidateRange(1, 10000)][int]$Keep = 7,
    [switch]$NoPrune,
    [string]$DumpExecutable
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot ".env"
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
    Write-Host "[ERR] Missing project .env"
    exit 1
}
$db = @{}
Get-Content -LiteralPath $envFile -Encoding UTF8 | ForEach-Object {
    if ($_ -match '^\s*DB_([A-Z_]+)\s*=\s*(.*?)\s*$') {
        $key = $matches[1]
        $value = $matches[2]
        if ($value.Length -ge 2 -and (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))
        )) { $value = $value.Substring(1, $value.Length - 2) }
        $db[$key] = $value
    }
}
# Environment settings take precedence, as in the application.
foreach ($key in @("DRIVER", "HOST", "PORT", "NAME", "WRITE_USER", "WRITE_PASSWORD")) {
    $override = [Environment]::GetEnvironmentVariable("DB_$key")
    if ($null -ne $override) { $db[$key] = $override }
}
if ($db.ContainsKey("DRIVER") -and $db["DRIVER"] -ne "mysql") {
    Write-Host "[ERR] This script only supports DB_DRIVER=mysql"
    exit 1
}
foreach ($key in @("HOST", "PORT", "NAME", "WRITE_USER", "WRITE_PASSWORD")) {
    if (-not $db.ContainsKey($key)) {
        Write-Host "[ERR] Missing DB_$key"
        exit 1
    }
}
if (-not $DumpExecutable) {
    $DumpExecutable = @(
        "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqldump.exe",
        "C:\Program Files (x86)\MySQL\MySQL Server 8.0\bin\mysqldump.exe"
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $DumpExecutable) {
        $command = Get-Command mysqldump -CommandType Application -ErrorAction SilentlyContinue
        if ($command) { $DumpExecutable = $command.Source }
    }
}
if (-not $DumpExecutable -or -not (Test-Path -LiteralPath $DumpExecutable -PathType Leaf)) {
    Write-Host "[ERR] mysqldump not found; check the MySQL client installation"
    exit 1
}
$backupDir = [IO.Path]::GetFullPath((Join-Path $projectRoot "backups"))
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
if ((Get-Item -LiteralPath $backupDir).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    Write-Host "[ERR] Refusing a linked backup directory"
    exit 1
}
$stamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
$suffix = [Guid]::NewGuid().ToString("N").Substring(0, 8)
$outFile = Join-Path $backupDir ("backup_" + $stamp + "_" + $suffix + ".sql")
$partialFile = "$outFile.partial"
$hadMysqlPwd = Test-Path Env:\MYSQL_PWD
$previousMysqlPwd = $env:MYSQL_PWD
$succeeded = $false
$env:MYSQL_PWD = $db["WRITE_PASSWORD"]
try {
    Write-Host "Exporting $($db['NAME']) @ $($db['HOST']):$($db['PORT'])"
    # Let mysqldump write bytes directly; avoid PowerShell text re-encoding.
    $dumpArgs = @(
        "--host=$($db['HOST'])", "--port=$($db['PORT'])", "--user=$($db['WRITE_USER'])",
        "--single-transaction", "--quick", "--no-tablespaces", "--set-gtid-purged=OFF",
        "--default-character-set=utf8mb4", "--result-file=$partialFile",
        "--databases", $db["NAME"]
    )
    # Do not assign a local LASTEXITCODE: it can shadow the native process result
    # when an executable is invoked through a child script.
    # Windows PowerShell can turn native stderr warnings into terminating errors.
    # Let the process finish, then decide from its exit code, not its stderr stream.
    $savedErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $DumpExecutable @dumpArgs 2>$null
        $dumpExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedErrorAction
    }
    if ($dumpExitCode -ne 0) { throw "mysqldump returned a nonzero exit code" }
    if (-not (Test-Path -LiteralPath $partialFile -PathType Leaf) -or
        (Get-Item -LiteralPath $partialFile).Length -eq 0) {
        throw "mysqldump did not produce a nonempty file"
    }
    foreach ($candidate in @($partialFile, $outFile)) {
        if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($candidate)) -ne $backupDir) {
            throw "Unsafe backup path"
        }
    }
    Move-Item -LiteralPath $partialFile -Destination $outFile -ErrorAction Stop
    $succeeded = $true
} catch {
    # Do not echo raw client errors, SQL rows, or credentials.
    Write-Host "[ERR] Backup failed; no valid .sql was published and old backups were not pruned."
    Write-Host "Check client compatibility, connection and dump privileges. A .partial file may remain."
} finally {
    if ($hadMysqlPwd) { $env:MYSQL_PWD = $previousMysqlPwd }
    else { Remove-Item Env:\MYSQL_PWD -ErrorAction SilentlyContinue }
}
if (-not $succeeded) { exit 1 }

Write-Host "[OK] Export completed: $outFile ($((Get-Item -LiteralPath $outFile).Length) bytes)"
Write-Host "This confirms export only; restoration has not been verified."
if ($NoPrune) {
    Write-Host "[INFO] NoPrune: all existing backups were kept."
    exit 0
}
# Retain regular, recognized .sql files only; never recurse or prune partial dumps.
try {
    $old = Get-ChildItem -LiteralPath $backupDir -File |
        Where-Object { $_.Name -match '^backup_\d{8}_\d{6}(?:_\d{3}_[0-9a-f]{8})?\.sql$' -and
            -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) } |
        Sort-Object Name -Descending | Select-Object -Skip $Keep
    foreach ($file in $old) {
        if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($file.FullName)) -ne $backupDir) {
            throw "Unsafe retention path"
        }
        if ($file.FullName -ne $outFile) { Remove-Item -LiteralPath $file.FullName -Force }
    }
} catch {
    Write-Host "[WARN] Export succeeded, but retention cleanup failed. The new backup is available."
    exit 2
}
exit 0
