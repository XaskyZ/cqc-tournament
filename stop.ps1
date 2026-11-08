$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
foreach ($name in @('website', 'telegram-bot')) {
    $pidFile = Join-Path $PSScriptRoot ('.local\' + $name + '.pid')
    if (-not (Test-Path -LiteralPath $pidFile)) { continue }
    $savedPid = [int](Get-Content -LiteralPath $pidFile)
    $running = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid"
    if (-not $running) { continue }
    $pattern = if ($name -eq 'website') { 'server\.py' } else { 'manage\.py.+run_bot' }
    if ($running.ExecutablePath -ne $pythonPath -or $running.CommandLine -notmatch $pattern) { throw ('Refusing to stop unrelated process: ' + $savedPid) }
    Stop-Process -Id $savedPid
    Write-Output ($name + ' stopped')
}
