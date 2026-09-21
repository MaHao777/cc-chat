param([string]$GoBinary = 'go')

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$version = '1.3.4'
$cache = Join-Path $projectRoot '.cache'
$archive = Join-Path $cache "cc-connect-v$version.zip"
$source = Join-Path $cache "cc-connect-$version"
$output = Join-Path $projectRoot 'runtime\bin\cc-connect.exe'
$patch = Join-Path $projectRoot 'patches\cc-connect-quiet-queue.patch'
New-Item -ItemType Directory -Force -Path $cache | Out-Null
if (-not (Test-Path -LiteralPath $archive)) {
    Invoke-WebRequest "https://codeload.github.com/chenhg5/cc-connect/zip/refs/tags/v$version" -OutFile $archive
}
$expected = '29CC8C619DC6DEC021441DC709F2F8F47F70CF2DEF0A091A9F47E8950F0B95B9'
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) {
    throw 'cc-connect source archive checksum mismatch'
}
if (-not (Test-Path -LiteralPath $source)) {
    Expand-Archive -LiteralPath $archive -DestinationPath $cache
}
# Isolate patch paths from the parent AICHAT repository (git otherwise skips them).
if (-not (Test-Path -LiteralPath (Join-Path $source '.git'))) {
    & git -C $source init --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Failed to initialize source patch workspace' }
}
& git -C $source apply --check $patch 2>$null
if ($LASTEXITCODE -eq 0) {
    & git -C $source apply $patch
    if ($LASTEXITCODE -ne 0) { throw 'Failed to apply queue patch' }
} else {
    & git -C $source apply --reverse --check $patch
    if ($LASTEXITCODE -ne 0) { throw 'Source does not match the expected queue patch' }
}
Copy-Item -LiteralPath (Join-Path $projectRoot 'patches\queue_quiet_test.go') `
    -Destination (Join-Path $source 'core\aichat_queue_quiet_test.go')
& $GoBinary -C $source test ./core -run 'TestAICHATQueueQuiet|TestQueueMessage|TestProcessInteractiveEvents_DrainsQueuedMessages|TestDrainOrphanedQueue' -count=1
if ($LASTEXITCODE -ne 0) { throw 'cc-connect queue regression tests failed' }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $output) | Out-Null
# AICHAT serves its own management UI; the upstream web bundle is not in source archives.
& $GoBinary -C $source build -tags no_web -trimpath -ldflags '-s -w -X main.version=v1.3.4-aichat.1' -o $output ./cmd/cc-connect
if ($LASTEXITCODE -ne 0) { throw 'cc-connect build failed' }
Write-Output "Built $output"
