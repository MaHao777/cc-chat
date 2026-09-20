$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
& "$projectRoot\.venv\Scripts\python.exe" -m cc_chat.cli start

