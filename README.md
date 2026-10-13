# DevOps Platform Demo

A small Flask service used to demonstrate repeatable container delivery, local Kubernetes operations, observability, and evidence-based incident triage. Flask is the only application workload. No Azure or AWS cloud resources are created.

## 1. Objective

Make the developer-to-operations path automated where it can be, observable during runtime, and explicit about what still requires a developer. GitHub-hosted CI validates and scans the image. Local CD rebuilds the checked-out app, stores it in LocalStack ECR, and deploys it to a dedicated Kind cluster. AI provides incident analysis only; scripts own recovery.

## 2. Architecture

```text
Developer push -> GitHub Actions CI -> tests -> Docker build -> Trivy scan
											  |
				  local workstation: Docker -> LocalStack ECR -> Kind
														 |         |
														 |         +-> Flask UI, /health, /metrics
														 |                    |          |
														 |                    |          +-> Prometheus
														 |                    +-> OTLP -> OpenTelemetry Collector
														 |
											  kubectl events/logs -> Python evidence -> Ollama RCA
																			  |
													  deterministic rollout rollback and health recheck
```

GitHub-hosted runners cannot reach a developer's `localhost` LocalStack or Kind cluster. CI and local CD are intentionally separate: run local CD after CI succeeds. A self-hosted runner on the developer network could automate that handoff later, but is not configured here.

## 3. Repository Structure

```text
app/                         Flask app, template, static assets, tests, requirements
Dockerfile                   Non-root Gunicorn container
kubernetes/                  Flask manifests and Kind registry/network config
localstack/docker-compose.yml LocalStack ECR service
observability/               Prometheus and OpenTelemetry configuration/workloads
scripts/                     Local CD, checks, rollback demo, logs, traces, AI RCA
terraform/main.tf             Terraform ownership of the observability namespace only
.github/workflows/ci.yml     Hosted test/build/Trivy workflow
```

## 4. Flask Application

The root page is a simple responsive DevOps Platform Demo UI showing application status, environment, version, health, and Kubernetes context. `GET /health` returns `{"status":"healthy"}`. `GET /metrics` exposes Prometheus counters and request-duration histograms. When `OTEL_EXPORTER_OTLP_ENDPOINT` is set, Flask server spans are exported over OTLP gRPC.

## 5. Local Setup and Tests

Use Python 3.12 or newer from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r app/requirements-dev.txt
$env:APP_ENVIRONMENT = "local"
$env:APP_VERSION = "dev"
python app/app.py
```

Open `http://localhost:8080/`; check `http://localhost:8080/health` and `http://localhost:8080/metrics`.

```powershell
python -m pytest app/test_app.py
python -m py_compile app/app.py app/test_app.py scripts/ai-rca.py scripts/log-analysis.py scripts/generate-trace.py
```

## 6. Docker

The runtime image uses `python:3.12-slim`, installs only runtime dependencies from `app/requirements.txt`, runs Gunicorn as a non-root user, and listens on port 8080. Pytest is kept in `app/requirements-dev.txt` and excluded from the image context.

```powershell
docker build --pull -t devops-platform:local .
docker run --rm -p 8080:8080 -e APP_ENVIRONMENT=container -e APP_VERSION=local devops-platform:local
```

In another PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8080/metrics
```

## 7. Kubernetes Manifests

`kubernetes/namespace.yaml`, `deployment.yaml`, and `service.yaml` own the Flask namespace, two-replica rolling Deployment, and NodePort Service. Both probes call `/health`; requests and limits are set. CI/CD replaces the image reference and release version at deploy time. Kind maps host port `8081` to Service node port `30080`.

```powershell
kubectl apply --dry-run=client -f kubernetes/namespace.yaml
kubectl apply --dry-run=client -f kubernetes/deployment.yaml
kubectl apply --dry-run=client -f kubernetes/service.yaml
```

## 8. Kind

The local workflow creates a separate cluster named `flask-platform` using `kubernetes/kind-config.yaml`; it does not replace the existing `devops-lab` cluster. The Kind node has a containerd HTTP registry mirror for `localhost.localstack.cloud:4510`, resolved inside Docker's `kind` network to the LocalStack container.

## 9. Terraform

Terraform manages exactly one Kubernetes resource: the `observability` namespace in context `kind-flask-platform`. The Flask namespace and all Deployments/Services are owned by Kubernetes YAML and `kubectl`; Terraform does not manage those objects. This splits ownership by resource and avoids competing controllers. Local Terraform state is written to the ignored `.local/terraform.tfstate` path; former tracked state snapshots were removed after their resources were destroyed from the original local cluster.

```powershell
terraform -chdir=terraform fmt -check
terraform -chdir=terraform init -reconfigure -input=false
terraform -chdir=terraform validate
terraform -chdir=terraform plan
terraform -chdir=terraform apply
```

Review the plan before applying if you change the configured Kind context. `scripts/cd_local.py setup` initializes Terraform and applies only the namespace resource.

## 10. LocalStack ECR

The local image flow is Docker build -> LocalStack ECR -> Kind. LocalStack uses `ECR_ENDPOINT_STRATEGY=off`, so ECR returns `localhost.localstack.cloud:4510/<repository>`; Compose publishes port 4510 and gives the same hostname to the `kind` Docker network. The current official LocalStack image requires a developer Auth Token associated with an eligible Base plan. No token is stored in this repository, and no AWS account or paid cloud resource is used. Obtain your own token through LocalStack and enter it into the current PowerShell process without placing it in command history:

```powershell
$secureToken = Read-Host -Prompt "LocalStack developer token" -AsSecureString
$env:LOCALSTACK_AUTH_TOKEN = [System.Net.NetworkCredential]::new("", $secureToken).Password
```

Then start the local platform and deploy:

```powershell
python scripts/cd_local.py setup
python scripts/cd_local.py deploy
```

Setup starts Kind and LocalStack, creates the `devops-platform` ECR repository, and applies the Terraform-owned namespace. Deploy builds a local tag, obtains a LocalStack ECR login token using the AWS CLI container with LocalStack-only dummy credentials, pushes to `localhost.localstack.cloud:4510`, creates an ephemeral Kubernetes pull secret, and deploys the same registry URI through the Kind network alias.

To inspect images after a successful push:

```powershell
docker run --rm --network kind --env AWS_ACCESS_KEY_ID=test --env AWS_SECRET_ACCESS_KEY=test --env AWS_DEFAULT_REGION=us-east-1 public.ecr.aws/aws-cli/aws-cli:2.37.9 ecr describe-images --repository-name devops-platform --endpoint-url http://localstack:4566 --region us-east-1
```

The current environment has no LocalStack Auth Token, so LocalStack startup, ECR push, and Kind registry pull could not be validated here. The setup script fails early with this exact prerequisite; it does not substitute fake credentials.

## 11. GitHub Actions CI

`.github/workflows/ci.yml` runs on pushes and pull requests targeting `main`. It checks out the commit, installs `app/requirements-dev.txt`, runs pytest, builds `devops-platform:<commit-sha>`, and fails on HIGH or CRITICAL Trivy findings. The image is local to the GitHub-hosted runner; it is not pushed to LocalStack, because hosted runners cannot address a workstation's local services. CI uses no registry or cloud secrets.

## 12. Local CD

There is no GitHub-hosted CD or Azure OIDC workflow. Local CD runs on the developer's machine:

```powershell
python scripts/cd_local.py deploy
```

It builds and pushes to LocalStack ECR, applies Flask and observability resources, waits for rollouts, then checks `http://127.0.0.1:8081/health`. Use `-ImageTag <tag>` to supply a specific local tag. Local CD is not triggered automatically by GitHub Actions.

## 13. Deployment Failure and Rollback

After a rollout or health failure, `scripts/deployment_check.py` collects pod state, deployment description, namespace events, and logs into an ignored `.local/incidents/` evidence snapshot. It performs deterministic `kubectl rollout undo`, waits for the previous revision, and rechecks health before asking Ollama to analyze the saved failure evidence. A slow or unavailable model cannot block recovery; malformed or unavailable model output reports the captured Kubernetes evidence without fabricating an RCA. The failed release still exits nonzero to keep the failure visible. AI output is never executed.

With a healthy release deployed, run the real failure demonstration:

```powershell
python scripts/cd_local.py rollback-demo
```

It deploys a deliberately nonexistent LocalStack ECR tag, expects image-pull/rollout failure, collects evidence, attempts AI analysis, rolls back, and verifies the prior image and health endpoint. This requires LocalStack and the initial successful deployment.

## 14. Python Automation

`scripts/cd_local.py` supports `setup`, `deploy`, `check`, `logs`, `metrics`, `traces`, `rca`, `rollback-demo`, and `destroy`. `scripts/deployment_check.py` checks rollout and external health, gathers diagnostics, and rolls back on failure. `scripts/log-analysis.py` filters current Flask logs for common error patterns. `scripts/ai-rca.py` gathers actual Kubernetes evidence and requests read-only recommendations from Ollama. `scripts/generate-trace.py` emits an OTLP span. Python helpers return nonzero exit codes on operational failures.

## 15. Logs

```powershell
kubectl get pods -n flask-app -o wide
kubectl logs -n flask-app -l app.kubernetes.io/name=flask-app --all-containers=true --tail=100
python scripts/log-analysis.py --namespace flask-app --since 1h
```

## 16. Prometheus Metrics

The Flask app exports `flask_http_requests_total` and `flask_http_request_duration_seconds` on `/metrics`. Prometheus scrapes the Flask Service DNS name. Local CD creates the ConfigMap from `observability/prometheus/prometheus.yml` and applies its Deployment and Service.

```powershell
kubectl port-forward -n observability svc/prometheus 9090:9090
```

Open `http://localhost:9090/graph` and query `flask_http_requests_total`.

## 17. OpenTelemetry Traces

The Flask server exports OTLP gRPC spans to the Collector Service. The Collector's debug exporter writes received spans to its pod logs; this demo has no trace-storage or trace-UI backend.

Terminal 1:

```powershell
kubectl port-forward -n observability svc/otel-collector 4317:4317
```

Terminal 2:

```powershell
$env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4317"
python scripts/generate-trace.py
kubectl logs -n observability deployment/otel-collector --since=5m
```

## 18. AI-Assisted RCA and Ollama

The RCA script sends pod phase/container state, deployment state, events, and application logs to the local Ollama API. It asks the model to cite evidence, explicitly recognize common Kubernetes image/rollout failures, avoid unsupported diagnoses, and recommend but never execute recovery.

Install and start Ollama separately, then fetch the model:

```powershell
ollama pull llama3.2:3b
ollama serve
```

In another terminal after deployment:

```powershell
python scripts/ai-rca.py --namespace flask-app
```

Use `--evidence-only` to collect evidence without contacting the model. Use `--evidence-file <path>` to analyze a saved incident snapshot. If Ollama is unavailable or returns an incomplete format, the script prints the captured evidence and reports that no RCA was generated. Override `OLLAMA_URL` or `OLLAMA_MODEL` through environment variables if needed.

## 19. Platform Engineering Workflow

Common operations are grouped in one Python entry point instead of requiring developers to remember separate sequences. The workstation owns Kind, LocalStack, Terraform namespace setup, local image promotion, health verification, observability access, incident evidence, and recovery. Required local tools are Python 3.12, Docker Desktop/Compose, Kind, kubectl, Terraform, and Ollama for model-based RCA. LocalStack ECR currently also requires the user's own Auth Token.

## 20. Security and Manual vs Automated Process

Manual workflow: test by hand -> build by hand -> create/tag the container -> deploy -> inspect Kubernetes -> search logs -> diagnose -> recover.

Automated path: Git push -> CI tests -> Docker build -> Trivy scan -> local developer runs CD -> LocalStack ECR -> Kind rollout -> health validation -> Prometheus/OTel -> evidence-based RCA -> deterministic rollback.

This removes repeatable manual steps but does not claim that a GitHub-hosted runner deploys to a private workstation cluster. No cloud credentials are stored in Git. The LocalStack auth token stays in the developer's process environment; the generated Kubernetes image-pull secret is local to the Kind cluster. The `test` AWS keys in the CLI container are emulator-only placeholders and are not valid AWS credentials.

## 21. Troubleshooting

- LocalStack exits with “License activation failed”: set your own `LOCALSTACK_AUTH_TOKEN` in the PowerShell session before running `setup`; do not commit it.
- `ImagePullBackOff`: inspect `kubectl describe pod -n flask-app`, verify the ECR push completed, and confirm the LocalStack container is attached to Docker network `kind` under alias `localstack`.
- Prometheus has no target: check `kubectl get endpoints -n flask-app flask-app`, the ConfigMap contents, and `http://localhost:9090/targets`.
- No traces: ensure the Collector is ready, port 4317 is forwarded, the app has `OTEL_EXPORTER_OTLP_ENDPOINT`, and inspect Collector logs.
- Ollama RCA unavailable: evidence is still printed; start Ollama, pull `llama3.2:3b`, and check `OLLAMA_URL`.
- Terraform context error: setup creates/selects `kind-flask-platform`; inspect with `kubectl config current-context` before running Terraform.

## 22. Manager Demo (10–15 Minutes)

1. **Application UI (1 min):** open `http://localhost:8080/` for local Flask, or `http://localhost:8081/` after local CD.
2. **Tests and Docker (1 min):** run `python -m pytest app/test_app.py`, then `docker build -t devops-platform:demo .`.
3. **Kind and Terraform (2 min):** show `kind get clusters`, `kubectl config current-context`, and `terraform -chdir=terraform plan`.
4. **CI (1 min):** in GitHub open **Actions → Flask CI → latest run** and show pytest, Docker build, and Trivy results.
5. **LocalStack ECR (1 min):** run `docker run --rm --network kind --env AWS_ACCESS_KEY_ID=test --env AWS_SECRET_ACCESS_KEY=test --env AWS_DEFAULT_REGION=us-east-1 public.ecr.aws/aws-cli/aws-cli:2.37.9 ecr describe-images --repository-name devops-platform --endpoint-url http://localstack:4566 --region us-east-1`.
6. **Local CD and Kubernetes (2 min):** run `python scripts/cd_local.py check` and `kubectl get deployments,services -n flask-app`; open `http://localhost:8081/`.
7. **Health and logs (1 min):** run `Invoke-RestMethod http://localhost:8081/health` and `python scripts/log-analysis.py`.
8. **Prometheus (1 min):** run `kubectl port-forward -n observability svc/prometheus 9090:9090`; open `http://localhost:9090/graph` and query `flask_http_requests_total`.
9. **Traces (1 min):** run the OTLP port-forward and `python scripts/generate-trace.py`; show the span in `kubectl logs -n observability deployment/otel-collector --since=5m`.
10. **Failure and RCA (2–3 min):** ensure Ollama is running, then run `python scripts/cd_local.py rollback-demo`; show image-pull evidence, the RCA recommendation, rollout undo, and a healthy response.

## 23. Cleanup

```powershell
python scripts/cd_local.py destroy
```

This destroys Terraform's `observability` namespace, stops the LocalStack Compose service, and deletes only the dedicated `flask-platform` Kind cluster. It does not delete the pre-existing `devops-lab` cluster or the user's Azurite container.
