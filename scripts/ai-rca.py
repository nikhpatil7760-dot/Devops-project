import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request


def image_identity(image):
    """Normalize Kubernetes' implicit Docker Hub library prefix for comparison."""
    return image.removeprefix("docker.io/library/")


def image_drift_issues(configured_images, pod_images):
    """Return issues when running containers do not match the requested image."""
    issues = []
    for container_name, configured_image in configured_images.items():
        observed_images = pod_images.get(container_name, set())
        if observed_images and image_identity(configured_image) not in {
            image_identity(image) for image in observed_images
        }:
            observed = ", ".join(sorted(observed_images))
            issues.append(
                f"container {container_name} is running image {observed}, "
                f"not configured image {configured_image}"
            )
    return issues

def kubectl(*arguments):
    result = subprocess.run(
        ["kubectl", *arguments], capture_output=True, text=True, check=False
    )
    if result.returncode:
        return f"Command failed ({result.returncode}): {result.stderr.strip()}"
    return result.stdout.strip() or "No output."


def collect_evidence(namespace, selector):
    pod_json = kubectl(
        "get", "pods", "-n", namespace, "-l", selector, "-o", "json"
    )
    deployment_json = kubectl(
        "get", "deployment", "flask-app", "-n", namespace, "-o", "json"
    )
    try:
        pods = json.loads(pod_json).get("items", [])
    except json.JSONDecodeError:
        pods = []
    deployment_spec = {}
    try:
        deployment = json.loads(deployment_json)
        deployment_spec = deployment.get("spec", {})
        deployment_status = deployment.get("status", {})
        desired = int(deployment_spec.get("replicas", 1))
        ready = int(deployment_status.get("readyReplicas", 0) or 0)
        available = int(deployment_status.get("availableReplicas", 0) or 0)
        updated = int(deployment_status.get("updatedReplicas", 0) or 0)
        container_images = [
            container.get("image", "unknown")
            for container in deployment_spec.get("template", {})
            .get("spec", {})
            .get("containers", [])
        ]
    except (json.JSONDecodeError, TypeError, ValueError):
        desired = ready = available = updated = 0
        container_images = []

    issues = []
    configured_images = {
        container.get("name", "unknown"): container.get("image", "unknown")
        for container in deployment_spec.get("template", {})
        .get("spec", {})
        .get("containers", [])
    }
    pod_images = {}
    if not deployment_json.startswith("{"):
        issues.append("deployment state could not be read")
    elif ready != desired or available != desired or updated != desired:
        issues.append(
            f"replica status differs from desired: desired={desired}, updated={updated}, ready={ready}, available={available}"
        )
    if not pods:
        issues.append("no matching application pods were found")

    events_json = kubectl("get", "events", "-n", namespace, "-o", "json")
    try:
        events = json.loads(events_json).get("items", [])
        events.sort(
            key=lambda event: event.get("lastTimestamp")
            or event.get("eventTime")
            or event.get("metadata", {}).get("creationTimestamp", "")
        )
        warnings = [event for event in events if event.get("type") == "Warning"]
        selected_events = warnings[-12:] if warnings else events[-6:]
        event_lines = [
            " ".join(
                [
                    event.get("type", "Normal"),
                    event.get("reason", "Unknown"),
                    f"{event.get('involvedObject', {}).get('kind', 'Object')}/{event.get('involvedObject', {}).get('name', 'unknown')}:",
                    event.get("message", ""),
                ]
            )
            for event in selected_events
        ]
        events_summary = "\n".join(event_lines) or "No Kubernetes events recorded."
    except (json.JSONDecodeError, AttributeError, TypeError):
        events_summary = events_json

    sections = [
        f"NAMESPACE: {namespace}",
        "DEPLOYMENT HEALTH SUMMARY:",
        f"desired={desired}, updated={updated}, ready={ready}, available={available}",
        f"configuredImages={container_images}",
        "PODS:",
    ]
    if not pods:
        sections.append(pod_json)
    for pod in pods:
        metadata = pod.get("metadata", {})
        status = pod.get("status", {})
        containers = status.get("containerStatuses", [])
        for container in containers:
            name = container.get("name", "unknown")
            pod_images.setdefault(name, set()).add(container.get("image", "unknown"))
            state = container.get("state", {})
            waiting = state.get("waiting")
            terminated = state.get("terminated")
            last_terminated = container.get("lastState", {}).get("terminated")
            if status.get("phase") != "Running":
                issues.append(
                    f"pod {metadata.get('name', 'unknown')} phase is {status.get('phase', 'unknown')}"
                )
            if waiting:
                issues.append(
                    f"container {name} waiting: {waiting.get('reason', 'unknown')}: {waiting.get('message', '')}"
                )
            elif not container.get("ready", False):
                issues.append(f"container {name} is not ready")
            if terminated and terminated.get("exitCode", 0) != 0:
                issues.append(
                    f"container {name} terminated with exitCode={terminated.get('exitCode')} reason={terminated.get('reason', 'unknown')}"
                )
            if last_terminated and last_terminated.get("exitCode", 0) != 0:
                issues.append(
                    f"container {name} previously terminated with exitCode={last_terminated.get('exitCode')} reason={last_terminated.get('reason', 'unknown')}"
                )
        container_statuses = [
            {
                "name": container.get("name"),
                "image": container.get("image"),
                "ready": container.get("ready"),
                "restartCount": container.get("restartCount"),
                "state": container.get("state"),
                "lastState": container.get("lastState"),
            }
            for container in status.get("containerStatuses", [])
        ]
        sections.append(
            "\n".join(
                [
                    f"{metadata.get('name', 'unknown')}: phase={status.get('phase', 'unknown')}",
                    "containerStatuses=" + json.dumps(container_statuses, separators=(",", ":")),
                    "LOGS:\n"
                    + kubectl(
                        "logs",
                        metadata.get("name", ""),
                        "-n",
                        namespace,
                        "--all-containers=true",
                        "--tail=20",
                    ),
                ]
            )
        )
    issues.extend(image_drift_issues(configured_images, pod_images))
    sections.extend(
        [
            "EVENTS:",
            events_summary,
        ]
    )
    if issues:
        sections.insert(1, "STATUS DETERMINATION: DEGRADED\n" + "\n".join(f"- {issue}" for issue in dict.fromkeys(issues)))
    else:
        sections.insert(
            1,
            f"STATUS DETERMINATION: HEALTHY\nAll {desired} desired replicas are updated, ready, and available; observed pods are Running and ready. No active incident or rollback condition is present.",
        )
    return "\n\n".join(sections)


def fallback_report(evidence, reason):
    """Create a short evidence-specific report when Ollama cannot provide an RCA."""
    if "Kubernetes deployment was not attempted" in evidence:
        return "\n".join([
            "Incident: Image loading failed before deployment.",
            "Evidence: The runtime command output is saved in the incident artifact; Kubernetes deployment was skipped.",
            "Root Cause: The image-loading command failed, so the existing application was not changed.",
            "Next Step: Correct the image-loading problem and rerun the normal deployment.",
            "Recovery: No rollback was required.",
        ])
    if "not configured image" in evidence:
        detail = next((line.strip() for line in evidence.splitlines() if "not configured image" in line), "The running image differs from the configured image.")
        return "\n".join([
            "Incident: The deployment is running a different image than the configured version.",
            f"Evidence: {detail}",
            "Root Cause: The deployment configuration and running image are out of sync.",
            "Next Step: Check the image tag used by the deployment workflow, then rerun deployment and health checks.",
            "Recovery: No verified recovery action was recorded.",
        ])
    if "ErrImagePull" in evidence or "ImagePullBackOff" in evidence:
        return "\n".join([
            "Incident: Kubernetes could not download the application image.",
            "Evidence: The incident contains an image-pull error from Kubernetes.",
            "Root Cause: The image is unavailable, incorrectly named, or the registry cannot be reached.",
            "Next Step: Verify the image name, registry access, and pull credentials, then rerun deployment.",
            "Recovery: No verified recovery action was recorded.",
        ])
    if "Readiness probe failed" in evidence or "not ready" in evidence:
        return "\n".join([
            "Incident: The application did not pass its readiness check.",
            "Evidence: Kubernetes recorded a readiness failure in the incident evidence.",
            "Root Cause: The application was not ready to receive traffic when Kubernetes checked it.",
            "Next Step: Review application logs and the readiness configuration, then rerun the health check.",
            "Recovery: No verified recovery action was recorded.",
        ])
    if "rollback=SUCCEEDED" in evidence:
        recovery = "Recovery: The previous version was restored and health-checked successfully."
    elif "rollback=FAILED" in evidence:
        recovery = "Recovery: Rollback was attempted but was not verified; manual investigation is required."
    else:
        recovery = "Recovery: No verified recovery result was recorded."
    return "\n".join([
        "Incident: Deployment or application health checks reported a failure.",
        "Evidence: The relevant logs, events, and pod state are saved in the incident artifact.",
        "Root Cause: The available evidence does not prove one specific cause.",
        "Next Step: Review the artifact, fix the confirmed issue, and rerun the health check.",
        recovery,
    ])


def report_without_rca(evidence, reason, report_output=None):
    """Report collected evidence without inventing an RCA."""
    report = fallback_report(evidence, reason)
    print(reason, file=sys.stderr)
    if report_output:
        report_path = Path(report_output)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
    else:
        print("\n=== Short incident report (deterministic fallback) ===")
        print(report)
def enforce_recovery_recommendation(analysis, evidence):
    """Keep Ollama from contradicting the recovery script's verified result."""
    if "Kubernetes deployment was not attempted" in evidence:
        recommendation = (
            "The intentional image-load step failed before Kubernetes deployment, so the "
            "existing application was left running and no rollback was required."
        )
    elif "rollback=SUCCEEDED" in evidence:
        recommendation = (
            "The failed rollout was rejected, and the previous deployment revision "
            "was restored and health-checked successfully by the recovery script."
        )
    elif "rollback=FAILED" in evidence:
        recommendation = (
            "The rollout failed, but the recovery script could not confirm that the "
            "previous deployment revision was restored and healthy. Manual recovery is required."
        )
    else:
        return analysis

    return re.sub(
        r"(?ims)^Rollback Recommendation:\s*.*\Z",
        f"Rollback Recommendation: {recommendation}",
        analysis.strip(),
        count=1,
    )

def has_required_sections(analysis):
    labels = (
        "Incident",
        "Evidence",
        "Root Cause",
        "Recommended Remediation",
        "Rollback Recommendation",
    )
    return all(
        re.search(rf"(?im)^\s*(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*:\s*\S+", analysis)
        for label in labels
    )


def compact_text(text, limit):
    """Keep prompts within the local model context while preserving both ends."""
    if len(text) <= limit:
        return text
    half = max(1, (limit - 80) // 2)
    return text[:half] + "\n... [middle omitted from Ollama prompt] ...\n" + text[-half:]


def collect_repository_context():
    """Read a small, relevant repository snapshot without executing or modifying it."""
    root = Path(__file__).resolve().parents[1]
    relative_files = [
        ".github/workflows/cicd.yml",
        "scripts/deployment_check.py",
        "kubernetes/deployment.yaml",
        "Dockerfile",
        "app/app.py",
    ]
    sections = []
    for relative_file in relative_files:
        path = root / relative_file
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as error:
            sections.append(f"--- {relative_file} ---\nUnavailable: {error}")
            continue
        sections.append(f"--- {relative_file} ---\n{compact_text(content, 1800)}")
    return compact_text("\n\n".join(sections), 8000)

def main():
    parser = argparse.ArgumentParser(
        description="Collect Kubernetes evidence and ask local Ollama for read-only RCA."
    )
    parser.add_argument("--namespace", default="flask-app")
    parser.add_argument("--selector", default="app.kubernetes.io/name=flask-app")
    parser.add_argument("--evidence-only", action="store_true")
    parser.add_argument("--evidence-file", help="Analyze a previously captured evidence snapshot.")
    parser.add_argument("--evidence-output", help="Write collected evidence to this UTF-8 file.")
    parser.add_argument("--report-output", help="Write the final RCA or fallback report to this UTF-8 file.")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()

    try:
        evidence = (
            Path(args.evidence_file).read_text(encoding="utf-8")
            if args.evidence_file
            else collect_evidence(args.namespace, args.selector)
        )
        if args.evidence_output:
            evidence_path = Path(args.evidence_output)
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(evidence, encoding="utf-8")
    except OSError as error:
        print(f"Could not read or save incident evidence: {error}", file=sys.stderr)
        return 1
    if args.evidence_only:
        print("=== Incident evidence ===")
        print(evidence)
        return 0

    repository_context = collect_repository_context()
    prompt_evidence = compact_text(evidence, 8000)
    prompt = f"""You are writing a short incident report for a human operator. Analyze the incident evidence and the read-only repository context below.
Use plain, direct language. Explain technical terms briefly when necessary. Do not invent causes that are not shown in the evidence. The machine-generated STATUS DETERMINATION is authoritative.
If a PIPELINE FAILURE section is present, classify that CI/CD stage failure first. If it says Kubernetes deployment was not attempted, state that the existing application was not changed and do not describe the current Kubernetes state as a failure caused by this run. If the evidence says the deployment is degraded, clearly state what failed, what evidence proves it, and the safest remediation. If a RECOVERY ACTION section is present, it is authoritative: when rollback=SUCCEEDED, explicitly say that the previous version was restored and validated; never say that there is no previous version. Recovery is controlled by the deployment script; do not provide commands and do not claim that Ollama performed a rollback. Use simple, human-readable language. In Recommended Remediation, say whether a code or configuration change is needed; if it is, tell the operator to manually start the Ollama remediation workflow for review and approval, and do not claim that it already ran.

Return exactly five short labeled lines, one plain-language sentence per line, with no preamble and no bullet list:
Incident: ...
Evidence: ...
Root Cause: ...
Recommended Remediation: ...
Rollback Recommendation: ...

EVIDENCE:
{prompt_evidence}

REPOSITORY CONTEXT:
{repository_context}
"""
    endpoint = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
    model = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(
            {
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_ctx": 4096, "num_predict": 160, "temperature": 0},
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout_seconds) as response:
            analysis = json.load(response).get("response", "No response text returned.")
        if not has_required_sections(analysis):
            report_without_rca(
                evidence,
                "Ollama response did not follow the required evidence format; no RCA was generated.",
                args.report_output,
            )
            return 1
        analysis = enforce_recovery_recommendation(analysis, evidence)
        report = f"=== Ollama RCA ({model}) ===\n{analysis}"
        if args.report_output:
            report_path = Path(args.report_output)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report, encoding="utf-8")
        else:
            print(f"\n{report}")
        return 0
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        report_without_rca(
            evidence,
            f"Ollama analysis unavailable at {endpoint}: {error}. No RCA was generated.",
            args.report_output,
        )
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
