"""Local platform operations for Kind, LocalStack ECR, and observability."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLUSTER = "flask-platform"
CONTEXT = "kind-flask-platform"
REGISTRY = "localhost.localstack.cloud:4510"
AWS_IMAGE = "public.ecr.aws/aws-cli/aws-cli:2.37.9"
AWS_ENV = {
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "AWS_DEFAULT_REGION": "us-east-1",
}


def run(command: list[str], *, cwd: Path | None = None, capture: bool = False, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=capture,
        check=False,
    )
    if not capture:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
    return result


def checked(command: list[str], description: str, *, cwd: Path | None = None) -> str:
    result = run(command, cwd=cwd, capture=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode:
        raise RuntimeError(f"{description} failed with exit code {result.returncode}.")
    return (result.stdout or "").strip()


def aws_local(arguments: list[str], allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(AWS_ENV)
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", "kind", *sum((["--env", f"{key}={value}"] for key, value in AWS_ENV.items()), []), AWS_IMAGE, *arguments],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    if result.returncode and not allow_failure:
        raise RuntimeError(f"LocalStack AWS CLI command failed: {result.stderr.strip()}")
    return result


def localstack_health() -> tuple[bool, bool]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:4566/_localstack/health", timeout=3) as response:
            payload = json.load(response)
        status = payload.get("services", {}).get("ecr")
        return True, status in {"available", "running"}
    except (OSError, ValueError):
        return False, False


def initialize() -> None:
    clusters = checked(["kind", "get", "clusters"], "Kind cluster query").splitlines()
    if CLUSTER not in clusters:
        checked(
            [
                "kind",
                "create",
                "cluster",
                "--name",
                CLUSTER,
                "--config",
                str(ROOT / "kubernetes" / "kind-config.yaml"),
                "--wait",
                "120s",
            ],
            "Kind cluster creation",
        )
    checked(["kubectl", "config", "use-context", CONTEXT], "Selecting Kind context")
    checked(["docker", "pull", AWS_IMAGE], "AWS CLI helper image pull")

    gateway_reachable, ecr_healthy = localstack_health()
    if not ecr_healthy:
        if not os.environ.get("LOCALSTACK_AUTH_TOKEN"):
            if gateway_reachable:
                raise RuntimeError("LocalStack responds but ECR is disabled. Set LOCALSTACK_AUTH_TOKEN before setup.")
            raise RuntimeError("LocalStack is not running. Set LOCALSTACK_AUTH_TOKEN before setup.")
        checked(
            [
                "docker",
                "compose",
                "-f",
                str(ROOT / "localstack" / "docker-compose.yml"),
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "180",
            ],
            "LocalStack startup",
        )
    else:
        print("Reusing healthy LocalStack ECR; no auth token is needed again in this session.")

    repository = aws_local(
        [
            "ecr",
            "describe-repositories",
            "--repository-names",
            "devops-platform",
            "--endpoint-url",
            "http://localstack:4566",
            "--region",
            "us-east-1",
        ],
        allow_failure=True,
    )
    if repository.returncode:
        aws_local(
            [
                "ecr",
                "create-repository",
                "--repository-name",
                "devops-platform",
                "--endpoint-url",
                "http://localstack:4566",
                "--region",
                "us-east-1",
            ]
        )

    checked(["terraform", "init", "-reconfigure", "-input=false"], "Terraform initialization", cwd=ROOT / "terraform")
    checked(["terraform", "apply", "-auto-approve", "-input=false"], "Terraform observability namespace apply", cwd=ROOT / "terraform")


def local_ecr_token() -> str:
    result = aws_local(
        [
            "ecr",
            "get-login-password",
            "--endpoint-url",
            "http://localstack:4566",
            "--region",
            "us-east-1",
        ]
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("Could not obtain a LocalStack ECR token.")
    return token


def publish(image_tag: str | None) -> tuple[str, str]:
    tag = image_tag or datetime.now().strftime("local-%Y%m%d%H%M%S")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise ValueError("ImageTag must be a valid Docker tag.")
    token = local_ecr_token()
    checked(["docker", "login", "--username", "AWS", "--password-stdin", REGISTRY], "Docker login to LocalStack ECR", input_text=token + "\n")
    image = f"{REGISTRY}/devops-platform:{tag}"
    checked(["docker", "build", "--tag", image, str(ROOT)], "Docker build")
    checked(["docker", "push", image], "LocalStack ECR image push")
    return token, image


def apply_config_map(name: str, namespace: str, key: str, file_path: Path) -> None:
    manifest = checked(
        [
            "kubectl",
            "create",
            "configmap",
            name,
            "-n",
            namespace,
            f"--from-file={key}={file_path}",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        f"Render ConfigMap {name}",
    )
    result = run(["kubectl", "apply", "-f", "-"], input_text=manifest)
    if result.returncode:
        raise RuntimeError(f"Could not apply ConfigMap {name}.")


def apply_pull_secret(token: str) -> None:
    auth = base64.b64encode(f"AWS:{token}".encode()).decode()
    config = {"auths": {REGISTRY: {"username": "AWS", "password": token, "auth": auth}}}
    encoded = base64.b64encode(json.dumps(config, separators=(",", ":")).encode()).decode()
    manifest = f"""apiVersion: v1
kind: Secret
metadata:
  name: localstack-ecr
  namespace: flask-app
type: kubernetes.io/dockerconfigjson
data:
  .dockerconfigjson: {encoded}
"""
    result = run(["kubectl", "apply", "-f", "-"], input_text=manifest)
    if result.returncode:
        raise RuntimeError("Could not apply the LocalStack ECR pull secret.")


def deploy(image_tag: str | None) -> None:
    initialize()
    token, image = publish(image_tag)
    tag = image.rsplit(":", 1)[1]
    checked(["kubectl", "apply", "-f", str(ROOT / "kubernetes" / "namespace.yaml")], "Flask namespace apply")
    apply_pull_secret(token)
    checked(["kubectl", "apply", "-f", str(ROOT / "kubernetes" / "deployment.yaml")], "Flask Deployment apply")
    apply_config_map("otel-collector-config", "observability", "config.yaml", ROOT / "observability" / "otel-config.yaml")
    apply_config_map("prometheus-config", "observability", "prometheus.yml", ROOT / "observability" / "prometheus" / "prometheus.yml")
    checked(["kubectl", "apply", "-f", str(ROOT / "observability" / "otel-deployment.yaml")], "OpenTelemetry resources apply")
    checked(["kubectl", "apply", "-f", str(ROOT / "observability" / "prometheus" / "prometheus-deployment.yaml")], "Prometheus resources apply")
    checked(["kubectl", "set", "image", "deployment/flask-app", f"flask-app={image}", "-n", "flask-app"], "Flask image update")
    checked(["kubectl", "set", "env", "deployment/flask-app", f"APP_VERSION={tag}", "-n", "flask-app"], "Flask version update")
    checked(["kubectl", "apply", "-f", str(ROOT / "kubernetes" / "service.yaml")], "Flask Service apply")
    checked(["kubectl", "rollout", "status", "deployment/otel-collector", "-n", "observability", "--timeout=180s"], "OpenTelemetry rollout")
    checked(["kubectl", "rollout", "status", "deployment/prometheus", "-n", "observability", "--timeout=180s"], "Prometheus rollout")
    result = run([sys.executable, str(ROOT / "scripts" / "deployment_check.py")])
    if result.returncode:
        raise RuntimeError("Local CD failed health validation; diagnostics and rollback were attempted.")
    print(f"Local deployment SUCCEEDED: http://127.0.0.1:8081 (image tag: {tag})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["setup", "deploy", "check", "logs", "metrics", "traces", "rca", "rollback-demo", "destroy"], nargs="?", default="setup")
    parser.add_argument("--image-tag")
    args = parser.parse_args()
    try:
        if args.action == "setup":
            initialize()
            print("LocalStack ECR and Kind are ready. Run: python scripts/cd_local.py deploy")
        elif args.action == "deploy":
            deploy(args.image_tag)
        elif args.action == "check":
            return run([sys.executable, str(ROOT / "scripts" / "deployment_check.py")]).returncode
        elif args.action == "logs":
            return run([sys.executable, str(ROOT / "scripts" / "log-analysis.py")]).returncode
        elif args.action == "metrics":
            print("Prometheus UI: http://127.0.0.1:9090")
            return run(["kubectl", "port-forward", "-n", "observability", "svc/prometheus", "9090:9090"]).returncode
        elif args.action == "traces":
            print("OTLP gRPC collector on localhost:4317. In another terminal run: python scripts/generate-trace.py")
            return run(["kubectl", "port-forward", "-n", "observability", "svc/otel-collector", "4317:4317"]).returncode
        elif args.action == "rca":
            return run([sys.executable, str(ROOT / "scripts" / "ai-rca.py")]).returncode
        elif args.action == "rollback-demo":
            return run([sys.executable, str(ROOT / "scripts" / "test_rollback.py")]).returncode
        elif args.action == "destroy":
            checked(["terraform", "destroy", "-auto-approve", "-input=false"], "Terraform local namespace destroy", cwd=ROOT / "terraform")
            checked(["docker", "compose", "-f", str(ROOT / "localstack" / "docker-compose.yml"), "down"], "LocalStack shutdown")
            if CLUSTER in run(["kind", "get", "clusters"], capture=True).stdout.splitlines():
                checked(["kind", "delete", "cluster", "--name", CLUSTER], "Kind cluster deletion")
            print("Local Flask platform resources removed.")
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Local CD failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
