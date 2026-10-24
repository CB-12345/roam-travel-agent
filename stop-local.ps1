$ErrorActionPreference = 'Stop'
$taskProject = $PSScriptRoot
$taskPidFile = Join-Path $taskProject 'data\local-runtime\server.pid'
if (-not (Test-Path -LiteralPath $taskPidFile)) {
    Write-Output 'No recorded local server.'
    exit 0
}
$taskServerId = [int](Get-Content -LiteralPath $taskPidFile -Raw).Trim()
$taskServer = Get-CimInstance Win32_Process -Filter "ProcessId = $taskServerId"
if ($taskServer) {
    $taskCommand = [string]$taskServer.CommandLine
    if (-not ($taskCommand.Contains($taskProject) -and $taskCommand.Contains('uvicorn') -and $taskCommand.Contains('main:app'))) {
        throw 'The recorded process no longer matches this project. No process was stopped.'
    }
    $taskChildren = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $taskServerId" | Where-Object {
        $taskChildCommand = [string]$_.CommandLine
        $taskChildCommand.Contains($taskProject) -and $taskChildCommand.Contains('uvicorn') -and $taskChildCommand.Contains('main:app')
    })
    foreach ($taskChild in $taskChildren) {
        if (Get-Process -Id $taskChild.ProcessId -ErrorAction SilentlyContinue) {
            Stop-Process -Id $taskChild.ProcessId
        }
    }
    if (Get-Process -Id $taskServerId -ErrorAction SilentlyContinue) {
        Stop-Process -Id $taskServerId
    }
    Write-Output 'Local server stopped.'
} else {
    Write-Output 'Local server is already stopped.'
}
Remove-Item -LiteralPath $taskPidFile
