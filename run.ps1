param(
    [ValidateSet('replay', 'test', 'credentials-set', 'demo-discover')]
    [string]$Action = 'replay'
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonPath = 'C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
}
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Python 3.12 not found. Create .venv with Python 3.12 first.'
}
if ($Action -eq 'test') {
    & $pythonPath -m unittest discover -s tests -v
} elseif ($Action -eq 'replay') {
    $runDirectory = Join-Path 'runtime' ('replay-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
    & $pythonPath -m gold_system replay --output $runDirectory
} else {
    & $pythonPath -m gold_system $Action
}
exit $LASTEXITCODE
