# PrZMA/przma_telegram_full_trigger.ps1
# Run full trigger on Telegram Web only (no TMI, no tool spec).
# Bootstrap opens Telegram -> Full Trigger runs on Telegram -> Main loop.
# Uses the public template for VM/discovery settings; the generic builder sets Telegram full-trigger fields.
#
# Prereqs: edit the template's VM paths/discovery values, set TELEGRAM_MEETING_CHAT in local .env,
# and prepare an authenticated Telegram Web session in the VM before launching.
#
# Example:
#   .\przma_telegram_full_trigger.ps1
#   .\przma_telegram_full_trigger.ps1 -RunId my_telegram_ft
param(
  [string]$RunId = "telegram_full_trigger_01",
  [string]$TemplateConfig = ".\Config(templete).json",
  [string]$Catalog = ".\Snapshot_Engine\artifact_catalog.json",
  [string]$OutDir = ".\runs",
  [string]$InterpreterOutRoot = ".\interpreter_out"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

$WorkDir = Join-Path $InterpreterOutRoot "telegram_full_trigger"
$ConfigPath = Join-Path $WorkDir "telegram_full_trigger_config.json"
$RulesPath = Join-Path $WorkDir "rules.json"

if (!(Test-Path $TemplateConfig)) {
  Write-Error "Template config not found: $TemplateConfig"
}

# 1) Build Telegram full-trigger config from the user-edited public template.
Write-Host "[*] Building Telegram full-trigger config (no tool spec)"
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$BuildScript = Join-Path $RepoRoot "scripts\build_full_trigger_config.py"
if (!(Test-Path $BuildScript)) { throw "Script not found: $BuildScript" }
python $BuildScript --platform telegram_web --template $TemplateConfig --output $ConfigPath
if (!(Test-Path $ConfigPath)) { throw "Config build failed: $ConfigPath not created" }
Write-Host "    $ConfigPath"

# 2) Run main.py (Bootstrap -> Full Trigger -> Main loop)
Write-Host "[*] Running PrZMA main.py (Telegram + full trigger only)"
python -u .\main.py `
  --config $ConfigPath `
  --run-id $RunId `
  --rules $RulesPath `
  --catalog $Catalog `
  --out-dir $OutDir

Write-Host "[*] Done. Run output: $OutDir\run_$RunId"
Write-Host "    Ensure TELEGRAM_MEETING_CHAT is set in .env (chat name/username)"
Write-Host "    Ensure the Telegram Web session is authenticated in the VM before launch."
