param(
    [string]$StateRoot = ""
)

$ErrorActionPreference = "Stop"

$stateRoot = if ([string]::IsNullOrWhiteSpace($StateRoot)) {
    Join-Path $env:LOCALAPPDATA "NWRCHStudio"
} else {
    $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($StateRoot)
}
$pidFile = Join-Path $stateRoot "server.pid"

if (-not (Test-Path $pidFile)) {
    Write-Host "Nenhum processo local registrado."
    exit 0
}

$pidText = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
if (-not $pidText) {
    Remove-Item -Force -ErrorAction SilentlyContinue $pidFile
    Write-Host "Arquivo de PID vazio removido."
    exit 0
}

$process = Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue
if ($process) {
    Stop-Process -Id $process.Id -Force
    Write-Host "NWRCH Studio local parado. PID: $($process.Id)"
} else {
    Write-Host "Processo local nao estava mais rodando. PID antigo: $pidText"
}

Remove-Item -Force -ErrorAction SilentlyContinue $pidFile
