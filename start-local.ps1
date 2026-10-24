param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$taskProject = $PSScriptRoot
$taskPython = Join-Path $taskProject '.venv\Scripts\python.exe'
$taskRuntime = Join-Path $taskProject 'data\local-runtime'
$taskPidFile = Join-Path $taskRuntime 'server.pid'
$taskUrl = 'http://127.0.0.1:8010'

if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'The project virtual environment is missing.'
}
New-Item -ItemType Directory -Path $taskRuntime -Force | Out-Null

$taskListener = Get-NetTCPConnection -State Listen -LocalPort 8010 -ErrorAction SilentlyContinue | Select-Object -First 1
if ($taskListener) {
    $taskExisting = Get-CimInstance Win32_Process -Filter "ProcessId = $($taskListener.OwningProcess)"
    $taskCommand = [string]$taskExisting.CommandLine
    if ($taskCommand.Contains($taskProject) -and $taskCommand.Contains('uvicorn') -and $taskCommand.Contains('main:app')) {
        Write-Output "Already running: $taskUrl"
        if (-not $NoBrowser) { Start-Process $taskUrl }
        exit 0
    }
    throw 'Port 8010 is occupied by another process. No process was stopped.'
}

$taskServer = Start-Process -FilePath $taskPython `
    -ArgumentList @('-u', '-X', 'utf8', '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', '8010') `
    -WorkingDirectory $taskProject `
    -RedirectStandardOutput (Join-Path $taskRuntime 'server.stdout.log') `
    -RedirectStandardError (Join-Path $taskRuntime 'server.stderr.log') `
    -WindowStyle Hidden -PassThru
$taskServer.Id | Set-Content -LiteralPath $taskPidFile -Encoding ascii

for ($taskAttempt = 0; $taskAttempt -lt 60; $taskAttempt++) {
    try {
        $taskResponse = Invoke-WebRequest -UseBasicParsing -Uri "$taskUrl/api/health" -TimeoutSec 2
        if ($taskResponse.StatusCode -eq 200) {
            Write-Output "Running: $taskUrl"
            if (-not $NoBrowser) { Start-Process $taskUrl }
            exit 0
        }
    } catch {
        $taskServer.Refresh()
        if ($taskServer.HasExited) {
            throw "The server exited. Check $taskRuntime\server.stderr.log"
        }
        Start-Sleep -Milliseconds 500
    }
}
throw "The server did not become ready. Check $taskRuntime\server.stderr.log"
