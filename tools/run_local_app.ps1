param(
    [int]$Port = 8787,
    [string]$HostName = "127.0.0.1",
    [string]$StateRoot = "",
    [switch]$NoInstall,
    [switch]$UseRepoData,
    [switch]$NoBrowser,
    [switch]$SelfTest
)

$ErrorActionPreference = "Stop"

function Resolve-Python {
    $localPython = Join-Path $env:LOCALAPPDATA "Python\bin\python.exe"
    $candidates = @(
        @{ File = $localPython; Args = @() },
        @{ File = "py"; Args = @("-3") },
        @{ File = "python"; Args = @() }
    )

    foreach ($candidate in $candidates) {
        try {
            & $candidate.File @($candidate.Args) --version *> $null
            if ($LASTEXITCODE -eq 0) {
                return $candidate
            }
        } catch {
            continue
        }
    }

    throw "Python nao encontrado. Instale Python 3.11+ ou ajuste o PATH."
}

function Test-Health {
    param([string]$Url)
    try {
        $response = Invoke-WebRequest -Uri "$Url/health" -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$stateRoot = if ([string]::IsNullOrWhiteSpace($StateRoot)) {
    Join-Path $env:LOCALAPPDATA "NWRCHStudio"
} else {
    $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($StateRoot)
}
$pidFile = Join-Path $stateRoot "server.pid"
$stdoutLog = Join-Path $stateRoot "server.out.log"
$stderrLog = Join-Path $stateRoot "server.err.log"
$dataDir = if ($UseRepoData) { Join-Path $repoRoot "data" } else { Join-Path $stateRoot "data" }
$venvDir = Join-Path $repoRoot ".venv-local"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$url = "http://$HostName`:$Port"

New-Item -ItemType Directory -Force -Path $stateRoot, $dataDir | Out-Null

if ($SelfTest) {
    $python = Resolve-Python
    Write-Host "OK: repo=$repoRoot"
    Write-Host "OK: python=$($python.File) $($python.Args -join ' ')"
    Write-Host "OK: data=$dataDir"
    Write-Host "OK: url=$url"
    exit 0
}

if (Test-Path $pidFile) {
    $existingPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($existingPid) {
        $existingProcess = Get-Process -Id ([int]$existingPid) -ErrorAction SilentlyContinue
        if ($existingProcess -and (Test-Health $url)) {
            if (-not $NoBrowser) {
                Start-Process $url
            }
            Write-Host "NWRCH Studio ja esta rodando em $url"
            exit 0
        }
    }
}

if (Test-Health $url) {
    if (-not $NoBrowser) {
        Start-Process $url
    }
    Write-Host "NWRCH Studio ja responde em $url"
    exit 0
}

if (-not (Test-Path $venvPython)) {
    $python = Resolve-Python
    Write-Host "Criando ambiente local em $venvDir ..."
    & $python.File @($python.Args) -m venv $venvDir
}

$depsOk = $false
try {
    & $venvPython -c "import importlib, importlib.util; import fastapi, uvicorn, jinja2, multipart, requests, httpx; raise SystemExit(0 if importlib.util.find_spec('kaggle') else 1)" *> $null
    $depsOk = $LASTEXITCODE -eq 0
} catch {
    $depsOk = $false
}

if (-not $depsOk) {
    if ($NoInstall) {
        throw "Dependencias ausentes em .venv-local. Rode sem -NoInstall para instalar requirements.txt."
    }
    Write-Host "Instalando dependencias locais ..."
    & $venvPython -m pip install -r (Join-Path $repoRoot "requirements.txt")
}

$env:APP_ENV = "dev"
$env:ENFORCE_CSRF = "0"
$env:ALLOW_REGISTRATION = "1"
$env:ALLOW_FIRST_USER = "1"
$env:APP_SECRET_KEY = "local-dev-only-secret-key-please-do-not-use-in-production"
$env:DATA_DIR = $dataDir
if ([string]::IsNullOrWhiteSpace($env:KAGGLE_UPLOAD_TIMEOUT_SECONDS)) {
    $env:KAGGLE_UPLOAD_TIMEOUT_SECONDS = "1800"
}

Remove-Item -Force -ErrorAction SilentlyContinue $stdoutLog, $stderrLog

Write-Host "Iniciando NWRCH Studio local em $url ..."
$process = Start-Process `
    -FilePath $venvPython `
    -ArgumentList @("-m", "uvicorn", "app:app", "--host", $HostName, "--port", "$Port") `
    -WorkingDirectory $repoRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru

Set-Content -Path $pidFile -Value $process.Id -Encoding ASCII

for ($i = 0; $i -lt 30; $i++) {
    if (Test-Health $url) {
        if (-not $NoBrowser) {
            Start-Process $url
            Write-Host "Aberto: $url"
        } else {
            Write-Host "Rodando: $url"
        }
        Write-Host "Para parar, rode NWRCH_Studio_Stop.bat"
        exit 0
    }
    Start-Sleep -Seconds 1
}

Write-Host "O servidor nao respondeu a tempo."
Write-Host "Log stdout: $stdoutLog"
Write-Host "Log stderr: $stderrLog"
exit 1
