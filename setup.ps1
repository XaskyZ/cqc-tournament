$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Python environment' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& '.\.venv\Scripts\python.exe' scripts/init_local.py
if ($LASTEXITCODE -ne 0) { throw 'Configuration failed' }
& '.\.venv\Scripts\python.exe' manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { throw 'Database migrations failed' }
& '.\.venv\Scripts\python.exe' manage.py bootstrap
if ($LASTEXITCODE -ne 0) { throw 'Bootstrap failed' }
& '.\.venv\Scripts\python.exe' manage.py collectstatic --noinput
if ($LASTEXITCODE -ne 0) { throw 'Static collection failed' }
Write-Output 'Ready. Website: .\.venv\Scripts\python.exe server.py'
Write-Output 'Bot (second terminal): .\.venv\Scripts\python.exe manage.py run_bot'
