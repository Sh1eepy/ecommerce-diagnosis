# Run isolated tests with the existing project interpreter, inside the sandbox.
# No installs, real database connections, model calls, or permission escalation.
$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$testsRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot 'tests'))
$runtimePath = [System.IO.Path]::GetFullPath((Join-Path $testsRoot '.pytest_runtime'))
if (-not $runtimePath.StartsWith($testsRoot + [System.IO.Path]::DirectorySeparatorChar)) {
    throw 'Unsafe test runtime path'
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Existing .venv interpreter is missing. No environment was created.'
}
$previousEncoding = $env:PYTHONIOENCODING
$testExitCode = 1
Push-Location -LiteralPath $projectRoot
try {
    $env:PYTHONIOENCODING = 'utf-8'
    # Cache is optional; old cache files may belong to an outside-sandbox run.
    # Keep all test assertions and strict deprecation checks enabled.
    & $pythonPath -m pytest -p no:cacheprovider -q -W error::DeprecationWarning @args
    $testExitCode = $LASTEXITCODE
} finally {
    $env:PYTHONIOENCODING = $previousEncoding
    Pop-Location
}
exit $testExitCode
