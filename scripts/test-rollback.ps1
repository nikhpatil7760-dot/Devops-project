[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 45,
    [string]$FailureImage = "localhost.localstack.cloud:4510/devops-platform:invalid-rollback-demo"
)

$ErrorActionPreference = "Stop"
$namespace = "flask-app"
$validImage = & kubectl get deployment flask-app -n $namespace -o jsonpath="{.spec.template.spec.containers[?(@.name=='flask-app')].image}"
if ($LASTEXITCODE -ne 0 -or -not $validImage) {
    throw "A deployed Flask release is required before running the rollback demonstration."
}

$health = Invoke-RestMethod -Uri "http://127.0.0.1:8081/health" -TimeoutSec 5
if ($health.status -ne "healthy") {
    throw "The current release is not healthy; refusing to start the rollback demonstration."
}

Write-Host "Deploying an intentionally invalid image to trigger ImagePullBackOff: $FailureImage"
& kubectl set image "deployment/flask-app" "flask-app=$FailureImage" -n $namespace
if ($LASTEXITCODE -ne 0) {
    throw "Could not apply the intentional failure image."
}

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "deployment-check.ps1") -TimeoutSeconds $TimeoutSeconds
$checkExitCode = $LASTEXITCODE
if ($checkExitCode -eq 0) {
    throw "The invalid image unexpectedly passed deployment validation."
}

$restoredImage = & kubectl get deployment flask-app -n $namespace -o jsonpath="{.spec.template.spec.containers[?(@.name=='flask-app')].image}"
if ($LASTEXITCODE -ne 0 -or $restoredImage -ne $validImage) {
    throw "The previous image was not restored. Expected '$validImage'; found '$restoredImage'."
}
$recovered = Invoke-RestMethod -Uri "http://127.0.0.1:8081/health" -TimeoutSec 10
if ($recovered.status -ne "healthy") {
    throw "Rollback completed but the application did not recover health."
}

Write-Host "Failure demonstration PASSED: invalid image detected, evidence collected, and $validImage recovered."