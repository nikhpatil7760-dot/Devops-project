"""Demonstrate an invalid image rollout and deterministic recovery."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, check=False)


def capture(command: list[str]) -> str:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    return (result.stdout or "").strip()


def is_healthy() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8081/health", timeout=5) as response:
            return json.load(response).get("status") == "healthy"
    except (OSError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-seconds", type=int, default=45)
    parser.add_argument(
        "--failure-image",
        default="localhost.localstack.cloud:4510/devops-platform:invalid-rollback-demo",
    )
    args = parser.parse_args()

    namespace = "flask-app"
    valid_image = capture(
        [
            "kubectl",
            "get",
            "deployment",
            "flask-app",
            "-n",
            namespace,
            "-o",
            "jsonpath={.spec.template.spec.containers[?(@.name=='flask-app')].image}",
        ]
    )
    if not valid_image:
        raise RuntimeError("A deployed Flask release is required before running the rollback demonstration.")
    if not is_healthy():
        raise RuntimeError("The current release is not healthy; refusing to start the rollback demonstration.")

    print(f"Deploying an intentionally invalid image to trigger ImagePullBackOff: {args.failure_image}")
    changed = run(
        [
            "kubectl",
            "set",
            "image",
            "deployment/flask-app",
            f"flask-app={args.failure_image}",
            "-n",
            namespace,
        ]
    )
    if changed.returncode != 0:
        raise RuntimeError("Could not apply the intentional failure image.")

    checker = run(
        [
            sys.executable,
            str(Path(__file__).with_name("deployment_check.py")),
            "--namespace",
            namespace,
            "--timeout-seconds",
            str(args.timeout_seconds),
        ]
    )
    if checker.returncode == 0:
        raise RuntimeError("The invalid image unexpectedly passed deployment validation.")

    restored_image = capture(
        [
            "kubectl",
            "get",
            "deployment",
            "flask-app",
            "-n",
            namespace,
            "-o",
            "jsonpath={.spec.template.spec.containers[?(@.name=='flask-app')].image}",
        ]
    )
    if restored_image != valid_image:
        raise RuntimeError(
            f"The previous image was not restored. Expected '{valid_image}'; found '{restored_image}'."
        )
    if not is_healthy():
        raise RuntimeError("Rollback completed but the application did not recover health.")

    print(
        "Failure demonstration PASSED: invalid image detected, evidence collected, "
        f"and {valid_image} recovered."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"Rollback demonstration failed: {error}", file=sys.stderr)
        raise SystemExit(1)
