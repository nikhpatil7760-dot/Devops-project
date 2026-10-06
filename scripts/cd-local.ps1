[CmdletBinding()]
param(
    [ValidateSet("setup", "deploy", "check", "logs", "metrics", "traces", "rca", "rollback-demo", "destroy")]
    [string]$Action = "setup",
    [string]$ImageTag
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$clusterName = "flask-platform"
$contextName = "kind-flask-platform"
$registryHost = "localhost.localstack.cloud:4510"
$clusterRegistryHost = "localhost.localstack.cloud:4510"
$awsImage = "public.ecr.aws/aws-cli/aws-cli:2.37.9"
$awsEnv = @(
    "--env", "AWS_ACCESS_KEY_ID=test",
    "--env", "AWS_SECRET_ACCESS_KEY=test",
    "--env", "AWS_DEFAULT_REGION=us-east-1"
)

function Invoke-CheckedCommand {
    param(
        [string]$Command,
        [string[]]$Arguments,
        [string]$Description
    )

    & $Command @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Invoke-AwsLocal {
    param(
        [string[]]$Arguments,
        [switch]$AllowFailure
    )

    $dockerArgs = @("run", "--rm", "--network", "kind") + $awsEnv + @($awsImage) + $Arguments
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & docker @dockerArgs 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }

    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "LocalStack AWS CLI command failed with exit code ${exitCode}: $($output | Out-String)"
    }
    return [pscustomobject]@{ ExitCode = $exitCode; Output = ($output | Out-String).Trim() }
}

function Initialize-LocalEnvironment {
    $clusters = @(& kind get clusters)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to query Kind clusters. Is Docker running?"
    }
    if ($clusters -notcontains $clusterName) {
        Invoke-CheckedCommand "kind" @(
            "create", "cluster", "--name", $clusterName,
            "--config", (Join-Path $repoRoot "kubernetes/kind-config.yaml"),
            "--wait", "120s"
        ) "Kind cluster creation"
    }

    Invoke-CheckedCommand "kubectl" @("config", "use-context", $contextName) "Selecting Kind context"
    Invoke-CheckedCommand "docker" @("pull", $awsImage) "AWS CLI helper image pull"
    $localstackGatewayReachable = $false
    $localstackHealthy = $false
    try {
        $localstackHealth = Invoke-RestMethod -Uri "http://127.0.0.1:4566/_localstack/health" -TimeoutSec 3
        $localstackGatewayReachable = $true
        $ecrStatus = $localstackHealth.services.ecr
        $localstackHealthy = $ecrStatus -in @("available", "running")
    } catch {
        $localstackHealthy = $false
    }

    if ($localstackHealthy) {
        Write-Host "Reusing healthy LocalStack ECR; no auth token is needed again in this session."
    } else {
        if (-not $env:LOCALSTACK_AUTH_TOKEN) {
            if ($localstackGatewayReachable) {
                throw "LocalStack responds but ECR is disabled (Community edition is running). Set your own LOCALSTACK_AUTH_TOKEN in this PowerShell session so Compose can replace it with the token-enabled image; do not commit it."
            }
            throw "LocalStack is not running. Set your own LOCALSTACK_AUTH_TOKEN in this PowerShell session before setup; do not commit it."
        }
        Invoke-CheckedCommand "docker" @(
            "compose", "-f", (Join-Path $repoRoot "localstack/docker-compose.yml"),
            "up", "--detach", "--wait", "--wait-timeout", "180"
        ) "LocalStack startup"
    }

    $repository = Invoke-AwsLocal -Arguments @(
        "ecr", "describe-repositories", "--repository-names", "devops-platform",
        "--endpoint-url", "http://localstack:4566", "--region", "us-east-1"
    ) -AllowFailure
    if ($repository.ExitCode -ne 0) {
        Invoke-AwsLocal -Arguments @(
            "ecr", "create-repository", "--repository-name", "devops-platform",
            "--endpoint-url", "http://localstack:4566", "--region", "us-east-1"
        ) | Out-Null
    }

    Push-Location (Join-Path $repoRoot "terraform")
    try {
        Invoke-CheckedCommand "terraform" @("init", "-reconfigure", "-input=false") "Terraform initialization"
        Invoke-CheckedCommand "terraform" @("apply", "-auto-approve", "-input=false") "Terraform observability namespace apply"
    } finally {
        Pop-Location
    }
}

function Get-LocalEcrToken {
    $tokenArgs = @("run", "--rm", "--network", "kind") + $awsEnv + @(
        $awsImage, "ecr", "get-login-password", "--endpoint-url", "http://localstack:4566", "--region", "us-east-1"
    )
    $tokenOutput = & docker @tokenArgs 2>$null
    $tokenExitCode = $LASTEXITCODE
    $token = ($tokenOutput | Out-String).Trim()
    if ($tokenExitCode -ne 0 -or -not $token) {
        throw "Could not obtain a LocalStack ECR token."
    }
    return $token
}

function Publish-LocalImage {
    if (-not $ImageTag) {
        $script:ImageTag = "local-$(Get-Date -Format yyyyMMddHHmmss)"
    }
    if ($ImageTag -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$') {
        throw "ImageTag must be a valid Docker tag."
    }

    $token = Get-LocalEcrToken
    $token | & docker login --username AWS --password-stdin $registryHost | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Docker login to LocalStack ECR failed."
    }

    $localImage = "${registryHost}/devops-platform:${ImageTag}"
    Invoke-CheckedCommand "docker" @("build", "--tag", $localImage, $repoRoot) "Docker build"
    Invoke-CheckedCommand "docker" @("push", $localImage) "LocalStack ECR image push"
    return @{ Token = $token; Image = "${clusterRegistryHost}/devops-platform:${ImageTag}" }
}

function Apply-ConfigMapFromFile {
    param([string]$Name, [string]$Namespace, [string]$Key, [string]$File)

    $manifest = & kubectl create configmap $Name -n $Namespace `
        "--from-file=${Key}=$File" --dry-run=client -o yaml
    if ($LASTEXITCODE -ne 0) {
        throw "Could not render ConfigMap $Name."
    }
    $manifest | & kubectl apply -f -
    if ($LASTEXITCODE -ne 0) {
        throw "Could not apply ConfigMap $Name."
    }
}

function Apply-EcrPullSecret {
    param([string]$Token)

    $auth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("AWS:$Token"))
    $dockerConfig = @{
        auths = @{
            $clusterRegistryHost = @{
                username = "AWS"
                password = $Token
                auth = $auth
            }
        }
    } | ConvertTo-Json -Compress -Depth 5
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($dockerConfig))
    $secretManifest = @"
apiVersion: v1
kind: Secret
metadata:
  name: localstack-ecr
  namespace: flask-app
type: kubernetes.io/dockerconfigjson
data:
  .dockerconfigjson: $encoded
"@
    $secretManifest | & kubectl apply -f -
    if ($LASTEXITCODE -ne 0) {
        throw "Could not apply the LocalStack ECR pull secret."
    }
}

function Deploy-LocalApplication {
    Initialize-LocalEnvironment
    $image = Publish-LocalImage

    Invoke-CheckedCommand "kubectl" @("apply", "-f", (Join-Path $repoRoot "kubernetes/namespace.yaml")) "Flask namespace apply"
    Apply-EcrPullSecret $image.Token
    Apply-ConfigMapFromFile "otel-collector-config" "observability" "config.yaml" (Join-Path $repoRoot "observability/otel-config.yaml")
    Apply-ConfigMapFromFile "prometheus-config" "observability" "prometheus.yml" (Join-Path $repoRoot "observability/prometheus/prometheus.yml")
    Invoke-CheckedCommand "kubectl" @("apply", "-f", (Join-Path $repoRoot "observability/otel-deployment.yaml")) "OpenTelemetry resources apply"
    Invoke-CheckedCommand "kubectl" @("apply", "-f", (Join-Path $repoRoot "observability/prometheus/prometheus-deployment.yaml")) "Prometheus resources apply"

    $deploymentFile = Join-Path $repoRoot "kubernetes/deployment.yaml"
    $rendered = & kubectl set image --local -f $deploymentFile "flask-app=$($image.Image)" -o yaml
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inject image reference into the Deployment."
    }
    $rendered = $rendered | & kubectl set env --local -f - "APP_VERSION=$ImageTag" -o yaml
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inject release version into the Deployment."
    }
    $rendered | & kubectl apply -f -
    if ($LASTEXITCODE -ne 0) {
        throw "Flask Deployment apply failed."
    }
    Invoke-CheckedCommand "kubectl" @("apply", "-f", (Join-Path $repoRoot "kubernetes/service.yaml")) "Flask Service apply"

    Invoke-CheckedCommand "kubectl" @("rollout", "status", "deployment/otel-collector", "-n", "observability", "--timeout=180s") "OpenTelemetry rollout"
    Invoke-CheckedCommand "kubectl" @("rollout", "status", "deployment/prometheus", "-n", "observability", "--timeout=180s") "Prometheus rollout"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "deployment-check.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Local CD failed health validation; diagnostics and rollback were attempted."
    }
    Write-Host "Local deployment SUCCEEDED: http://127.0.0.1:8081 (image tag: $ImageTag)"
}

switch ($Action) {
    "setup" {
        Initialize-LocalEnvironment
        Write-Host "LocalStack ECR and Kind are ready. Run: .\scripts\cd-local.ps1 deploy"
    }
    "deploy" { Deploy-LocalApplication }
    "check" {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "deployment-check.ps1")
        exit $LASTEXITCODE
    }
    "logs" {
        & python (Join-Path $PSScriptRoot "log-analysis.py")
        exit $LASTEXITCODE
    }
    "metrics" {
        Write-Host "Prometheus UI: http://127.0.0.1:9090"
        & kubectl port-forward -n observability svc/prometheus 9090:9090
    }
    "traces" {
        Write-Host "OTLP gRPC collector on localhost:4317. In another terminal run: python scripts/generate-trace.py"
        & kubectl port-forward -n observability svc/otel-collector 4317:4317
    }
    "rca" {
        & python (Join-Path $PSScriptRoot "ai-rca.py")
        exit $LASTEXITCODE
    }
    "rollback-demo" {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "test-rollback.ps1")
        exit $LASTEXITCODE
    }
    "destroy" {
        Push-Location (Join-Path $repoRoot "terraform")
        try {
            Invoke-CheckedCommand "terraform" @("destroy", "-auto-approve", "-input=false") "Terraform local namespace destroy"
        } finally {
            Pop-Location
        }
        Invoke-CheckedCommand "docker" @("compose", "-f", (Join-Path $repoRoot "localstack/docker-compose.yml"), "down") "LocalStack shutdown"
        $clusters = @(& kind get clusters)
        if ($clusters -contains $clusterName) {
            Invoke-CheckedCommand "kind" @("delete", "cluster", "--name", $clusterName) "Kind cluster deletion"
        }
        Write-Host "Local Flask platform resources removed."
    }
}