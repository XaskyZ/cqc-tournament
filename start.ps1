$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run .\setup.ps1 first' }
$services = @(
    @{ Name = 'website'; Args = @('-u', 'server.py'); Pattern = 'server\.py' },
    @{ Name = 'telegram-bot'; Args = @('-u', 'manage.py', 'run_bot'); Pattern = 'manage\.py.+run_bot' }
)
foreach ($service in $services) {
    if ($service.Name -eq 'telegram-bot' -and (Test-Path -LiteralPath '.local\cloud-webhook.json')) {
        Write-Output 'Telegram uses the hosted webhook. Use the public website for Telegram sign-in.'
        continue
    }
    $pidFile = Join-Path $PSScriptRoot ('.local\' + $service.Name + '.pid')
    if (Test-Path -LiteralPath $pidFile) {
        $savedPid = [int](Get-Content -LiteralPath $pidFile)
        $running = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid"
        if ($running) {
            if ($running.ExecutablePath -ne $pythonPath -or $running.CommandLine -notmatch $service.Pattern) { throw ('PID belongs to a different process: ' + $service.Name) }
            Write-Output ($service.Name + ' already running')
            continue
        }
    }
    $process = Start-Process -FilePath $pythonPath -ArgumentList $service.Args -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput ('.local\' + $service.Name + '.log') -RedirectStandardError ('.local\' + $service.Name + '-error.log') -PassThru
    $process.Id | Set-Content -LiteralPath $pidFile
    Write-Output ($service.Name + ' started: ' + $process.Id)
}
Write-Output 'Website: http://127.0.0.1:8000/'
Write-Output 'Organizer credentials: .local/admin-access.txt'
