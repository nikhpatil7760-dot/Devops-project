[CmdletBinding()]
param(
    [string]$Namespace = "flask-app",
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = "Continue"
$deployment = "flask-app"
$serviceUrl = "http://127.0.0.1:8081/health"
$healthy = $false

Write-Host "Checking rollout for $Namespace/$deployment..."
& kubectl rollout status "deployment/$deployment" -n $Namespace --timeout="${TimeoutSeconds}s"
if ($LASTEXITCODE -eq 0) {
    $replicas = & kubectl get deployment $deployment -n $Namespace -o jsonpath="{.status.readyReplicas}/{.spec.replicas}"
    Write-Host "Ready replicas: $replicas"
    try {
        $response = Invoke-RestMethod -Uri $serviceUrl -TimeoutSec 5
        $healthy = $response.status -eq "healthy"
        if ($healthy) {
            Write-Host "Health check passed: $serviceUrl"
        }
    } catch {
        Write-Host "Health check failed: $($_.Exception.Message)"
    }
}

if ($healthy) {
    Write-Host "Deployment validation PASSED."
    exit 0
}

Write-Host "Deployment validation FAILED. Collecting diagnostics..."
& kubectl get pods -n $Namespace -o wide
& kubectl describe deployment $deployment -n $Namespace
& kubectl get events -n $Namespace --sort-by=.lastTimestamp
& kubectl logs -n $Namespace -l app.kubernetes.io/name=flask-app --all-containers=true --tail=100

$repoRoot = Split-Path -Parent $PSScriptRoot
$incidentDirectory = Join-Path $repoRoot ".local/incidents"
$incidentId = [guid]::NewGuid().ToString("N")
$evidencePath = Join-Path $incidentDirectory "incident-$incidentId.txt"
& python (Join-Path $repoRoot "scripts/ai-rca.py") `
    --namespace $Namespace --evidence-only --evidence-output $evidencePath
if ($LASTEXITCODE -ne 0) {
    Write-Host "Evidence collection failed; attempting rollback regardless."
}

Write-Host "Attempting deterministic rollout rollback..."
$recovered = $false
& kubectl rollout undo "deployment/$deployment" -n $Namespace
if ($LASTEXITCODE -eq 0) {
    & kubectl rollout status "deployment/$deployment" -n $Namespace --timeout="${TimeoutSeconds}s"
    if ($LASTEXITCODE -eq 0) {
        try {
            $healthResponse = Invoke-RestMethod -Uri $serviceUrl -TimeoutSec 5
            $recovered = $healthResponse.status -eq "healthy"
        } catch {
            Write-Host "Rollback completed but health validation failed: $($_.Exception.Message)"
        }
    }
}

if ($recovered) {
    Write-Host "Rollback recovery PASSED; the prior application revision is healthy."
} else {
    Write-Host "Rollback did not restore a healthy revision."
}

if (Test-Path $evidencePath) {
    Write-Host "Incident evidence saved locally: $evidencePath"
    Write-Host "Running bounded Ollama analysis after recovery; this cannot delay rollback."
    & python (Join-Path $repoRoot "scripts/ai-rca.py") `
        --evidence-file $evidencePath --timeout-seconds 180
    if ($LASTEXITCODE -ne 0) {
        Write-Host "AI analysis was unavailable; the evidence snapshot is retained for later review."
    }
}

exit 1