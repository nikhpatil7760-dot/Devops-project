import subprocess
import requests


def run(cmd):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True
    ).stdout


pods = run([
    "kubectl", "get", "pods",
    "-n", "podinfo",
    "-o", "jsonpath={.items[*].metadata.name}"
]).split()


for pod in pods:

    pod_status = run([
        "kubectl", "get", "pod", pod,
        "-n", "podinfo",
        "-o", "jsonpath={.status.phase}"
    ])

    pod_reason = run([
        "kubectl", "get", "pod", pod,
        "-n", "podinfo",
        "-o", "jsonpath={.status.containerStatuses[*].state}"
    ])

    events = run([
        "kubectl", "get", "events",
        "-n", "podinfo",
        "--field-selector", f"involvedObject.name={pod}",
        "--sort-by=.lastTimestamp"
    ])

    logs = run([
        "kubectl", "logs", pod,
        "-n", "podinfo",
        "--tail=100"
    ])

    prompt = f"""
You are a Kubernetes SRE incident-analysis assistant.

Analyze ONLY the evidence below.

POD:
{pod}

PHASE:
{pod_status}

CONTAINER STATE:
{pod_reason}

KUBERNETES EVENTS:
{events}

APPLICATION LOGS:
{logs}

Give the answer in exactly this format:

Incident:
Evidence:
Root Cause:
Recommended Remediation:
Rollback Recommendation:

Important:
- If the evidence contains ImagePullBackOff or ErrImagePull, identify it explicitly.
- If the image/tag is invalid, state that as the root cause.
- Do not invent networking, service mesh, CPU, memory, or application problems unless evidence supports them.
"""

    response = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": "llama3.2:3b",
            "prompt": prompt,
            "stream": False
        }
    )

    print(f"\n===== AI RCA: {pod} =====")
    print(response.json()["response"])