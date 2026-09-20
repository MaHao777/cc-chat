$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$cc = Join-Path $env:APPDATA 'npm\node_modules\cc-connect\bin\cc-connect.exe'
$config = Join-Path $env:USERPROFILE '.cc-connect\config.toml'
& $python -m cc_chat.cli start | Out-File -FilePath (Join-Path $projectRoot 'data\start.log') -Append -Encoding utf8
$running = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cc-connect.exe' -and $_.CommandLine -like "*$config*" }
if (-not $running) {
  Start-Process -FilePath $cc -ArgumentList @('--config', $config) -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $projectRoot 'data\cc-connect.stdout.log') `
    -RedirectStandardError (Join-Path $projectRoot 'data\cc-connect.stderr.log')
}

