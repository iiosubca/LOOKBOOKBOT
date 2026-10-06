param(
    [Parameter(Mandatory=$true)][string]$Executable,
    [Parameter(Mandatory=$true)][string]$Output,
    [ValidateRange(20, 60)][int]$TimeoutSeconds = 60
)
$ErrorActionPreference = 'Stop'
$exe = (Resolve-Path -LiteralPath $Executable).Path
$isolated = [System.IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $isolated) { throw 'Use a fresh startup-test directory.' }
New-Item -ItemType Directory -Path $isolated | Out-Null
$oldData = $env:LOCALAPPDATA
$oldPlatform = $env:QT_QPA_PLATFORM
$process = $null
try {
    $env:LOCALAPPDATA = $isolated
    $env:QT_QPA_PLATFORM = 'offscreen'
    $process = Start-Process -FilePath $exe -WindowStyle Hidden -PassThru
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $database = Join-Path $isolated 'LOOKBOOKBOT\lookbookbot.db'
    # A clean one-file build must first unpack Qt. Antivirus and concurrent
    # native integration tests can make a cold extraction exceed 20 seconds.
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        $process.Refresh()
        if ($process.HasExited) { throw "Packaged application exited before startup verification: $($process.ExitCode)" }
        if ((Test-Path -LiteralPath $database) -and $timer.Elapsed.TotalSeconds -ge 6) { break }
        Start-Sleep -Milliseconds 300
    }
    if (-not (Test-Path -LiteralPath $database)) { throw 'Packaged GUI did not initialize its isolated application database.' }
    $children = @(Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $process.Id -and $_.Name -eq 'LOOKBOOKBOT.exe' })
    if (-not $children) { throw 'Packaged application child is not running after startup.' }
    Write-Output 'PASS packaged startup: Qt imported, application database initialized; production settings untouched.'
} finally {
    if ($null -ne $process) {
        # Stop only the test application created above, never an existing bot.
        Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $process.Id -and $_.Name -eq 'LOOKBOOKBOT.exe' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
        if (-not $process.HasExited) { Stop-Process -Id $process.Id -ErrorAction SilentlyContinue }
    }
    $env:LOCALAPPDATA = $oldData
    $env:QT_QPA_PLATFORM = $oldPlatform
}
