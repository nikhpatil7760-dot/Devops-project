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
    """Create a readable report when Ollama cannot provide a validated RCA."""
    if "Kubernetes deployment was not attempted" in evidence:
        cause = "The image-loading stage failed before Kubernetes deployment; the existing application was not changed by this run."
        impact = "No user-facing impact from this deployment attempt was demonstrated."
        recovery = "No recovery was required because Kubernetes deployment was not attempted."
        next_step = "Correct the image-loading failure, rerun the normal scenario, and verify the exact image tag."
    elif "rollback=SUCCEEDED" in evidence:
        cause = "The rollout failed according to the captured deployment evidence; review the events and pod state for the specific cause."
        impact = "The attempted release was not retained; the previous deployment revision was restored."
        recovery = "Recovery succeeded and was verified: the previous deployment revision was restored and health-checked."
        next_step = "Review the captured events and logs, correct the release cause, and validate the next rollout."
    elif "rollback=FAILED" in evidence:
        cause = "The rollout failed, and the available evidence does not confirm a healthy restored revision."
        impact = "Application availability is not confirmed and requires human investigation."
        recovery = "Recovery was attempted but failed or remains unverified."
        next_step = "Inspect the deployment, pods, events, and logs, then restore and verify a known-good revision."
    else:
        cause = "The available evidence is insufficient to confirm one root cause."
        impact = "The evidence does not establish the full user-facing impact."
        recovery = "Recovery remains unverified because no authoritative recovery result was captured."
        next_step = "Review the captured events and logs, correct the confirmed failure, and rerun health checks."
    return "\n\n".join([
        "Incident Summary: A CI/CD, deployment, or application health incident was reported.",
        "What Happened: The captured evidence records the failed stage and observed system state.",
        f"Root Cause: {cause}",
        f"Impact: {impact}",
        "What Succeeded Before the Failure: Evidence collection completed; earlier successful stages are recorded in the workflow logs.",
        "Actions Taken: Evidence was collected and deterministic recovery results were recorded when recovery was attempted.",
        f"Recovery Status: {recovery}",
        f"What Should I Do Next: {next_step}",
        "Remaining Issues: Ollama did not provide a validated analysis, so unsupported conclusions are omitted.",
        f"Evidence: {reason}\n\n{evidence}",
    ])


def report_without_rca(evidence, reason, report_output=None):
    """Report collected evidence without inventing an RCA."""
    report = fallback_report(evidence, reason)
    print(reason, file=sys.stderr)
    print("\n=== Incident report (deterministic fallback) ===")
    print(report)
    if report_output:
        report_path = Path(report_output)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")

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

    prompt = f"""You are writing an incident report for a human operator. Analyze only the Kubernetes evidence below.
Use plain, direct language. Explain technical terms briefly when necessary. Do not invent causes that are not shown in the evidence. The machine-generated STATUS DETERMINATION is authoritative.
If a PIPELINE FAILURE section is present, classify that CI/CD stage failure first. If it says Kubernetes deployment was not attempted, state that the existing application was not changed and do not describe the current Kubernetes state as a failure caused by this run. If the evidence says the deployment is degraded, clearly state what failed, what evidence proves it, and the safest remediation. If a RECOVERY ACTION section is present, it is authoritative: when rollback=SUCCEEDED, explicitly say that the previous version was restored and validated; never say that there is no previous version. Recovery is controlled by the deployment script; do not provide commands and do not claim that Ollama performed a rollback.

Return exactly these five labeled paragraphs, with two to four complete, readable sentences for each and no preamble:
Incident: ...
Evidence: ...
Root Cause: ...
Recommended Remediation: ...
Rollback Recommendation: ...

EVIDENCE:
{evidence}
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
                "options": {"num_ctx": 4096, "num_predict": 384, "temperature": 0},
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
        print(f"\n{report}")
        if args.report_output:
            report_path = Path(args.report_output)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report, encoding="utf-8")
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
