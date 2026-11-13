param(
    [int]$Port = 8082,
    [switch]$Live,
    [switch]$Autorun,
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:UR_DB_PATH = ":memory:"
$env:UR_WORLD_SCENARIO = "assets/scenarios/sheffield_reports_demo/scenario.json"
$env:UR_WORLD_OUTCOME_MODE = "model"
$env:UR_WORLD_AUTORUN = if ($Autorun) { "1" } else { "0" }
$env:UR_TIME_SCALE = "20"
$env:LLM_MODE = if ($Live) { "live" } else { "mock" }
Write-Host "SURU: http://localhost:$Port/console (fresh in-memory world; closing this process resets the run)"
Write-Host "Agents: $env:LLM_MODE. Outcome proposals are validated before graph effects."
& $Python -m uvicorn urban_response.api:app --host 127.0.0.1 --port $Port
