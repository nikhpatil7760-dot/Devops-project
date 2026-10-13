#!/usr/bin/env python3
"""
Validate a Flask rollout, collect diagnostics and recover failed releases.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
import uuid
from pathlib import Path


DEPLOYMENT_NAME = "flask-app"


def run(
    command: list[str],
    *,
    capture: bool = False,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Run a command without raising on a non-zero exit code."""
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=capture,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Command execution failed: {exc}", file=sys.stderr)
        return subprocess.CompletedProcess(
            args=command,
            returncode=1,
            stdout="",
            stderr=str(exc),
        )

    if not capture:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)

    return result


def output(command: list[str]) -> str:
    """Return command output, raising a useful error on failure."""
    result = run(command, capture=True)

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "Unknown error").strip()
        raise RuntimeError(message)

    return (result.stdout or "").strip()


def health_check(url: str) -> bool:
    """Check the application health endpoint."""
    if not url:
        return True

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            body = response.read().decode("utf-8", errors="replace")

            if response.status != 200:
                print(f"Health check returned HTTP {response.status}")
                return False

            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                # A successful HTTP response with a non-JSON body is acceptable.
                return True

            if not isinstance(payload, dict):
                return True

            status = str(payload.get("status", "")).lower()

            return (
                status in ("healthy", "ok", "up")
                or payload.get("healthy") is True
            )

    except Exception as exc:
        print(f"Health check failed: {exc}")
        return False


def deployment_ready(namespace: str) -> bool:
    """Check whether all desired replicas of the deployment are ready."""
    try:
        deployment_json = output(
            [
                "kubectl",
                "get",
                "deployment",
                DEPLOYMENT_NAME,
                "-n",
                namespace,
                "-o",
                "json",
            ]
        )

        deployment = json.loads(deployment_json)
        spec = deployment.get("spec", {})
        status = deployment.get("status", {})

        desired = spec.get("replicas", 1)
        ready = status.get("readyReplicas", 0)
        available = status.get("availableReplicas", 0)
        updated = status.get("updatedReplicas", 0)
        observed = status.get("observedGeneration", 0)
        generation = metadata_generation = deployment.get(
            "metadata", {}
        ).get("generation", 0)

        print(
            "Deployment status: "
            f"desired={desired}, "
            f"updated={updated}, "
            f"ready={ready}, "
            f"available={available}"
        )

        if observed < metadata_generation:
            print("Deployment controller has not observed the latest generation.")
            return False

        return (
            ready >= desired
            and available >= desired
            and updated >= desired
        )

    except Exception as exc:
        print(f"Unable to determine deployment health: {exc}")
        return False


def collect_diagnostics(namespace: str) -> None:
    """Collect diagnostics without letting missing Pods interrupt recovery."""
    commands = [
        (
            "PODS",
            [
                "kubectl",
                "get",
                "pods",
                "-n",
                namespace,
                "-o",
                "wide",
            ],
        ),
        (
            "DEPLOYMENT",
            [
                "kubectl",
                "describe",
                "deployment",
                DEPLOYMENT_NAME,
                "-n",
                namespace,
            ],
        ),
        (
            "EVENTS",
            [
                "kubectl",
                "get",
                "events",
                "-n",
                namespace,
                "--sort-by=.lastTimestamp",
            ],
        ),
        (
            "CURRENT DEPLOYMENT LOGS",
            [
                "kubectl",
                "logs",
                "-n",
                namespace,
                f"deployment/{DEPLOYMENT_NAME}",
                "--tail=200",
                "--all-containers=true",
            ],
        ),
        (
            "PREVIOUS CONTAINER LOGS",
            [
                "kubectl",
                "logs",
                "-n",
                namespace,
                "-l",
                "app.kubernetes.io/name=flask-app",
                "--previous",
                "--tail=100",
                "--all-containers=true",
            ],
        ),
    ]

    for title, command in commands:
        print(f"\n=== {title} ===")
        result = run(command, timeout=30)

        if result.returncode != 0:
            print(
                f"Diagnostic command failed; continuing recovery: "
                f"{' '.join(command)}"
            )


def rollback(namespace: str, timeout: int, health_url: str) -> bool:
    """Roll back to a previous revision and validate the recovered release."""
    print("\nAttempting deployment rollback...")

    history = run(
        [
            "kubectl",
            "rollout",
            "history",
            f"deployment/{DEPLOYMENT_NAME}",
            "-n",
            namespace,
        ],
        timeout=30,
    )

    if history.returncode != 0:
        print("Unable to retrieve rollout history; attempting rollback anyway.")

    undo = run(
        [
            "kubectl",
            "rollout",
            "undo",
            f"deployment/{DEPLOYMENT_NAME}",
            "-n",
            namespace,
        ],
        timeout=30,
    )

    if undo.returncode != 0:
        print("Rollback command failed.")
        return False

    rollout = run(
        [
            "kubectl",
            "rollout",
            "status",
            f"deployment/{DEPLOYMENT_NAME}",
            "-n",
            namespace,
            f"--timeout={timeout}s",
        ],
        timeout=timeout + 15,
    )

    if rollout.returncode != 0:
        print("Rolled-back deployment did not complete its rollout.")
        return False

    if not deployment_ready(namespace):
        print("Rolled-back deployment is not ready.")
        return False

    if health_url:
        print(f"Validating recovered application: {health_url}")

        # Allow a short period for the service to route to the recovered Pods.
        for attempt in range(1, 4):
            if health_check(health_url):
                print("Recovered application health check PASSED.")
                return True

            if attempt < 3:
                import time
                time.sleep(3)

        print("Recovered application health check FAILED.")
        return False

    return True


def collect_incident_evidence(namespace: str) -> Path | None:
    """Save incident evidence; evidence collection must not block recovery."""
    repo_root = Path(__file__).resolve().parents[1]
    incident_dir = repo_root / ".local" / "incidents"

    try:
        incident_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Unable to create incident directory: {exc}")
        return None

    evidence_path = incident_dir / f"incident-{uuid.uuid4().hex}.txt"

    result = run(
        [
            sys.executable,
            str(repo_root / "scripts" / "ai-rca.py"),
            "--namespace",
            namespace,
            "--evidence-only",
            "--evidence-output",
            str(evidence_path),
        ],
        timeout=60,
    )

    if result.returncode != 0:
        print("AI RCA evidence collection failed; continuing recovery.")
        return None

    if evidence_path.exists():
        print(f"Incident evidence saved: {evidence_path}")
        return evidence_path

    print("Evidence collection completed without creating the evidence file.")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a Flask deployment and recover failed releases."
    )

    parser.add_argument(
        "--namespace",
        default="flask-app",
    )

    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=120,
    )

    parser.add_argument(
        "--health-url",
        default="",
        help="Optional health endpoint URL",
    )

    args = parser.parse_args()

    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be greater than zero")

    print("Waiting for deployment rollout...")

    rollout = run(
        [
            "kubectl",
            "rollout",
            "status",
            f"deployment/{DEPLOYMENT_NAME}",
            "-n",
            args.namespace,
            f"--timeout={args.timeout_seconds}s",
        ],
        timeout=args.timeout_seconds + 15,
    )

    deployment_ok = (
        rollout.returncode == 0
        and deployment_ready(args.namespace)
    )

    service_ok = True

    if deployment_ok and args.health_url:
        print(f"Running health check: {args.health_url}")
        service_ok = health_check(args.health_url)

    if deployment_ok and service_ok:
        print("Deployment validation PASSED.")
        return 0

    print("Deployment validation FAILED.")

    # Diagnostic failures must not prevent the recovery attempt.
    collect_diagnostics(args.namespace)
    collect_incident_evidence(args.namespace)

    recovered = rollback(
        args.namespace,
        args.timeout_seconds,
        args.health_url,
    )

    if recovered:
        print("Rollback recovery PASSED.")
        print(
            "The previous release was restored and validated. "
            "The requested deployment validation had failed."
        )
    else:
        print("Rollback recovery FAILED.")
        print(
            "Manual investigation may be required. "
            "Check deployment status, events and incident evidence."
        )

    # Preserve a non-zero result for the original failed deployment.
    # This allows CI/CD to report that the release failed even if recovery worked.
    return 1


if __name__ == "__main__":
    raise SystemExit(main())