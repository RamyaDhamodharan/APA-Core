[CmdletBinding()]
param([switch]$KeepStack)

$RootRunner = Join-Path (Split-Path -Parent $PSScriptRoot) '..\scripts\run_e2e.ps1'
$ResolvedRunner = (Resolve-Path $RootRunner).Path
& $ResolvedRunner -KeepStack:$KeepStack
if (-not $?) { exit 1 }
exit 0
