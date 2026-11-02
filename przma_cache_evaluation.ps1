[CmdletBinding()]
param(
    [ValidateSet("Preflight","Smoke","Pilot","Freeze","Campaign","PairedReplay","Verify")]
    [string]$Mode = "Preflight",
    [ValidateRange(1,20)][int]$Count = 3,
    [string]$Config = ".\evaluation\local\evaluation.json",
    [string]$Selection = "",
    [string]$Evidence = ".\evaluation\evidence\final20",
    [string]$PilotSummary = "",
    [switch]$Approve,
    [int]$KeepServerOnFailureSeconds = 15
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
Set-Location -LiteralPath $RepositoryRoot

$ConfigModes = @("Preflight", "Smoke", "Pilot", "Campaign", "PairedReplay")
if ($Mode -in $ConfigModes -and -not (Test-Path -LiteralPath $Config -PathType Leaf)) {
    Write-Error @"
Evaluation Config is missing: $Config
Copy evaluation/cache_recovery/config.example.json to evaluation/local/evaluation.json,
replace every placeholder, or provide another local file with -Config.
"@
    exit 2
}

if ($Mode -eq "Freeze" -and -not $Approve) {
    Write-Error "Freeze requires explicit approval. Re-run with -Approve after reviewing the Pilot summary."
    exit 2
}

if ($Mode -eq "Freeze" -and -not $PilotSummary) {
    $LatestPilot = Get-ChildItem -LiteralPath ".\evaluation\results" -Filter "autonomous_pilot_summary_*.json" -File -ErrorAction SilentlyContinue |
        Sort-Object -Property LastWriteTimeUtc, Name -Descending |
        Select-Object -First 1
    if (-not $LatestPilot) {
        Write-Error "No autonomous Pilot summary was found in evaluation/results."
        exit 2
    }
    $PilotSummary = $LatestPilot.FullName
}

if ($Mode -eq "PairedReplay" -and -not $Selection) {
    $Selection = ".\evaluation\results\aggregate\final20_selection.json"
    if (-not (Test-Path -LiteralPath $Selection -PathType Leaf)) {
        Write-Error "PairedReplay selection is missing: $Selection"
        exit 2
    }
}

$Arguments = switch ($Mode) {
    "Preflight" {
        @("-m", "evaluation.cache_recovery.runner", "autonomous-preflight",
          "--config", $Config,
          "--keep-server-on-failure-seconds", $KeepServerOnFailureSeconds)
    }
    "Smoke" {
        @("-m", "evaluation.cache_recovery.runner", "autonomous-smoke",
          "--config", $Config,
          "--keep-server-on-failure-seconds", $KeepServerOnFailureSeconds)
    }
    "Pilot" {
        @("-m", "evaluation.cache_recovery.runner", "autonomous-pilot",
          "--count", $Count,
          "--config", $Config,
          "--keep-server-on-failure-seconds", $KeepServerOnFailureSeconds)
    }
    "Freeze" {
        @("-m", "evaluation.cache_recovery.freeze_campaign",
          "--pilot-summary", $PilotSummary,
          "--output", ".\evaluation\results\autonomous_freeze_manifest.json",
          "--approve")
    }
    "Campaign" {
        @("-m", "evaluation.cache_recovery.runner", "autonomous-campaign",
          "--config", $Config,
          "--keep-server-on-failure-seconds", $KeepServerOnFailureSeconds)
    }
    "PairedReplay" {
        @("-m", "evaluation.cache_recovery.runner", "paired-action-replay",
          "--selection", $Selection,
          "--config", $Config,
          "--keep-server-on-failure-seconds", $KeepServerOnFailureSeconds)
    }
    "Verify" {
        @("-m", "evaluation.cache_recovery.verify_final20",
          "--evidence", $Evidence)
    }
}

Write-Host "[PrZMA Cache Evaluation] Mode=$Mode"
if ($Mode -in @("Smoke", "Pilot", "Campaign", "PairedReplay")) {
    Write-Host "[PrZMA Cache Evaluation] A visible local Research Archive server is opened during execution."
}
if ($Mode -eq "Pilot") {
    Write-Host "[PrZMA Cache Evaluation] Pilot runs=$Count"
}
if ($Mode -eq "Campaign") {
    Write-Host "[PrZMA Cache Evaluation] Campaign is fixed at 20 held-out runs."
}

& python @Arguments
$PythonExitCode = $LASTEXITCODE
if ($PythonExitCode -ne 0) {
    Write-Error "Cache Recovery command failed with exit code $PythonExitCode."
    exit $PythonExitCode
}
exit 0
