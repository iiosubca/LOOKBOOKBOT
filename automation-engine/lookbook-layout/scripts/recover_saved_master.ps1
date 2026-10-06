[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Project)
$ErrorActionPreference = 'Stop'
function Result([string]$Status, [string]$Reason) {
    [pscustomobject]@{status=$Status; reason=$Reason} | ConvertTo-Json -Compress
    exit 0
}
$root = [IO.Path]::GetFullPath($Project)
$state = Get-Content -LiteralPath (Join-Path $root 'control\lookbook-state.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$master = [IO.Path]::GetFullPath((Join-Path $root ([string]$state.master)))
if ([IO.Path]::GetDirectoryName($master) -ne $root -or -not (Test-Path -LiteralPath $master -PathType Leaf)) {
    Result 'blocked' 'Controlled master path is not a direct project file.'
}
# Never close a document while another native worker may still be executing,
# including a worker orphaned by a Python/CLI timeout. The helper itself is
# not a gate worker and is explicitly excluded by PID.
$workers = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and $_.Name -in @('python.exe','powershell.exe','pwsh.exe') -and
    ($_.CommandLine -match '(?:^|\s)-File\s+"?[^"\r\n]*[\\/]run_lookbook_gate_com\.ps1(?:"|\s|$)' -or
     $_.CommandLine -match 'lookbook_gate\.py"?\s+(apply|apply-composition|export-pdf|publish-final)\b')
})
if ($workers.Count -gt 0) { Result 'busy' 'A native automation worker is still present.' }
$instances = @(Get-Process -Name InDesign -ErrorAction SilentlyContinue)
if ($instances.Count -ne 1 -or -not $instances[0].Responding) { Result 'blocked' 'One responsive InDesign instance is required.' }
if ([string]$instances[0].MainWindowTitle -notmatch '^Adobe InDesign(?: 2026)?$') {
    Result 'blocked' 'A visible operator document is not an abandoned hidden automation document.'
}
$before = Get-Item -LiteralPath $master
$signature = @($before.Length, $before.LastWriteTimeUtc.Ticks)
$app = New-Object -ComObject InDesign.Application
$matches = @($app.Documents | Where-Object { [IO.Path]::GetFullPath([string]$_.FullName) -eq $master })
if ($matches.Count -ne 1) { Result 'blocked' 'No unique open controlled master; lock cannot be deleted blindly.' }
$doc = $matches[0]
# Saved means 'saved at least once', not 'no unsaved changes'. Modified is the
# essential independent guard that the previous native helper omitted.
if (-not [bool]$doc.Saved -or [bool]$doc.Modified) { Result 'unsaved' 'Save and close the document manually; no changes discarded.' }
Start-Sleep -Milliseconds 500
$after = Get-Item -LiteralPath $master
if ($after.Length -ne $signature[0] -or $after.LastWriteTimeUtc.Ticks -ne $signature[1]) {
    Result 'busy' 'Master is still changing on disk.'
}
if ([bool]$doc.Modified) { Result 'unsaved' 'Document became modified during inspection.' }
$doc.Close(1852776480) # SaveOptions.NO, safe only after Modified=false.
Result 'closed' 'Closed only the exact saved hidden automation master; no files deleted.'
