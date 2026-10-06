"""Validate a Flask rollout, collect evidence, and recover failed releases."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=capture, check=False)
    if capture:
        return result
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result


def output(command: list[str]) -> str:
    result = run(command, capture=True)
    return (result.stdout or "").strip()


def healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.load(response)
        return payload.get("status") == "healthy"
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="flask-app")
    parser.add_argument("--timeout-seconds", type=int, default=90)
    args = parser.parse_args()

    deployment = "flask-app"
    service_url = "http://127.0.0.1:8081/health"
    rollout = run(
        [
            "kubectl",
            "rollout",
            "status",
            f"deployment/{deployment}",
            "-n",
            args.namespace,
            f"--timeout={args.timeout_seconds}s",
        ]
    )
    is_healthy = rollout.returncode == 0
    if is_healthy:
        replicas = output(
            [
                "kubectl",
                "get",
                "deployment",
                deployment,
                "-n",
                args.namespace,
                "-o",
                "jsonpath={.status.readyReplicas}/{.spec.replicas}",
            ]
        )
        print(f"Ready replicas: {replicas}")
        is_healthy = healthy(service_url)
        if is_healthy:
            print(f"Health check passed: {service_url}")

    if is_healthy:
        print("Deployment validation PASSED.")
        return 0

    print("Deployment validation FAILED. Collecting diagnostics...")
    run(["kubectl", "get", "pods", "-n", args.namespace, "-o", "wide"])
    run(["kubectl", "describe", "deployment", deployment, "-n", args.namespace])
    run(["kubectl", "get", "events", "-n", args.namespace, "--sort-by=.lastTimestamp"])
    run(
        [
            "kubectl",
            "logs",
            "-n",
            args.namespace,
            "-l",
            "app.kubernetes.io/name=flask-app",
            "--all-containers=true",
            "--tail=100",
        ]
    )

    repo_root = Path(__file__).resolve().parents[1]
    incident_dir = repo_root / ".local" / "incidents"
    incident_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = incident_dir / f"incident-{uuid.uuid4().hex}.txt"
    evidence_result = run(
        [
            sys.executable,
            str(repo_root / "scripts" / "ai-rca.py"),
            "--namespace",
            args.namespace,
            "--evidence-only",
            "--evidence-output",
            str(evidence_path),
        ]
    )
    if evidence_result.returncode != 0:
        print("Evidence collection failed; attempting rollback regardless.")

    print("Attempting deterministic rollout rollback...")
    recovered = False
    undo = run(["kubectl", "rollout", "undo", f"deployment/{deployment}", "-n", args.namespace])
    if undo.returncode == 0:
        restored = run(
            [
                "kubectl",
                "rollout",
                "status",
                f"deployment/{deployment}",
                "-n",
                args.namespace,
                f"--timeout={args.timeout_seconds}s",
            ]
        )
        recovered = restored.returncode == 0 and healthy(service_url)

    if recovered:
        print("Rollback recovery PASSED; the prior application revision is healthy.")
    else:
        print("Rollback did not restore a healthy revision.")

    if evidence_path.exists():
        print(f"Incident evidence saved locally: {evidence_path}")
        print("Running bounded Ollama analysis after recovery; this cannot delay rollback.")
        analysis = run(
            [
                sys.executable,
                str(repo_root / "scripts" / "ai-rca.py"),
                "--evidence-file",
                str(evidence_path),
                "--timeout-seconds",
                "180",
            ]
        )
        if analysis.returncode != 0:
            print("AI analysis was unavailable; the evidence snapshot is retained for later review.")

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
