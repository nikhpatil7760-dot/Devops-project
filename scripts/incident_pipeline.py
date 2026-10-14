"""Generic CI/CD failure evidence, investigation, and proposal primitives.

The stage runner is deliberately unaware of failure categories.  It records the
process that failed, while the investigator selects repository context from the
recorded evidence and asks Ollama for a report.  Neither component changes the
pipeline's original exit status.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
INCIDENT_DIR = ROOT / ".local" / "incidents"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def context() -> dict[str, str]:
    return {
        "commit_sha": os.getenv("GITHUB_SHA", "unknown"),
        "workflow_run_url": os.getenv("GITHUB_SERVER_URL", "")
        + ("/" + os.getenv("GITHUB_REPOSITORY", "") if os.getenv("GITHUB_REPOSITORY") else "")
        + ("/actions/runs/" + os.getenv("GITHUB_RUN_ID", "") if os.getenv("GITHUB_RUN_ID") else ""),
        "run_id": os.getenv("GITHUB_RUN_ID", "unknown"),
        "workflow": os.getenv("GITHUB_WORKFLOW", "unknown"),
        "event": os.getenv("GITHUB_EVENT_NAME", "unknown"),
    }


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")[:80] or "stage"


def run_stage(stage: str, command: list[str], action: str | None = None) -> int:
    """Run one pipeline action and persist raw, lossless stage evidence."""
    start = utc_now()
    try:
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
        stdout, stderr, code = completed.stdout, completed.stderr, completed.returncode
    except OSError as error:
        stdout, stderr, code = "", f"{type(error).__name__}: {error}\n", 127
    end = utc_now()
    INCIDENT_DIR.mkdir(parents=True, exist_ok=True)
    index = len(list((INCIDENT_DIR / "stages").glob("*.json"))) if (INCIDENT_DIR / "stages").exists() else 0
    stage_dir = INCIDENT_DIR / "stages"
    stage_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "stage": stage,
        "action": action or " ".join(command),
        "command": command,
        "started_at": start,
        "finished_at": end,
        "exit_status": code,
        "stdout": stdout,
        "stderr": stderr,
        "raw_log": stdout + stderr,
        "artifacts": [str(p.relative_to(ROOT)) for p in ROOT.glob(".local/**/*") if p.is_file()],
        **context(),
    }
    path = stage_dir / f"{index:04d}-{_safe_name(stage)}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    (INCIDENT_DIR / f"{index:04d}-{_safe_name(stage)}.log").write_text(record["raw_log"], encoding="utf-8")
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return code


def load_records(directory: Path = INCIDENT_DIR) -> list[dict[str, Any]]:
    records = []
    for path in sorted((directory / "stages").glob("*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return records


def _relevant_files(evidence: str, limit: int = 14) -> list[Path]:
    """Select context by evidence tokens; never send the entire repository."""
    candidates = [p for p in ROOT.rglob("*") if p.is_file() and not ({".git", ".local", ".venv", "__pycache__", ".pytest_cache"} & set(p.parts))]
    tokens = {t.lower() for t in re.findall(r"[A-Za-z0-9_./-]{3,}", evidence)}
    scored: list[tuple[int, Path]] = []
    for path in candidates:
        relative = path.relative_to(ROOT)
        score = sum(3 for token in tokens if token in str(relative).lower())
        if relative.parts[0] in {".github", "scripts", "app", "kubernetes", "terraform"}:
            score += 1
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            score += sum(1 for token in tokens if token in content.lower())
        except OSError:
            continue
        if score:
            scored.append((score, path))
    return [path for _, path in sorted(scored, key=lambda item: (-item[0], str(item[1])))[:limit]]


def collect_github_actions_logs(directory: Path) -> dict[str, Any]:
    """Collect the true failed step and job logs when the runner has a GitHub token."""
    repository = os.getenv("GITHUB_REPOSITORY")
    run_id = os.getenv("GITHUB_RUN_ID")
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    result: dict[str, Any] = {"failed_steps": [], "logs_available": False}
    if not repository or not run_id or not token:
        return result
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
    try:
        jobs_request = urllib.request.Request(f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/jobs?per_page=100", headers=headers)
        with urllib.request.urlopen(jobs_request, timeout=30) as response:
            jobs = json.loads(response.read().decode())
        for job in jobs.get("jobs", []):
            for step in job.get("steps", []):
                if step.get("conclusion") == "failure":
                    result["failed_steps"].append({"job": job.get("name"), "step": step.get("name"), "number": step.get("number")})
        (directory / "github-actions.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        job_ids = [job.get("id") for job in jobs.get("jobs", []) if job.get("id")]
        log_parts = []
        for job_id in job_ids:
            logs_request = urllib.request.Request(f"https://api.github.com/repos/{repository}/actions/jobs/{job_id}/logs", headers=headers)
            with urllib.request.urlopen(logs_request, timeout=60) as response:
                payload = response.read()
            try:
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    log_parts.extend(archive.read(name).decode("utf-8", errors="replace") for name in archive.namelist())
            except zipfile.BadZipFile:
                log_parts.append(payload.decode("utf-8", errors="replace"))
        if log_parts:
            (directory / "github-actions.log").write_text("\n\n".join(log_parts), encoding="utf-8")
            result["logs_available"] = True
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, zipfile.BadZipFile):
        # Evidence collection must never replace the original pipeline result.
        return result
    return result


def has_required_report_sections(report: str) -> bool:
    labels = ("Failure stage", "Evidence", "Root cause", "Impact", "Files examined", "proposed", "Tests", "verification")
    lowered = report.lower()
    return all(label.lower() in lowered for label in labels)

def build_incident(directory: Path = INCIDENT_DIR) -> tuple[Path, dict[str, Any]]:
    directory.mkdir(parents=True, exist_ok=True)
    github = collect_github_actions_logs(directory)
    jobs_file = directory / "github-actions-jobs.json"
    if jobs_file.exists():
        try:
            jobs_payload = json.loads(jobs_file.read_text(encoding="utf-8"))
            for job in jobs_payload.get("jobs", []):
                for step in job.get("steps", []):
                    if step.get("conclusion") == "failure":
                        github.setdefault("failed_steps", []).append({"job": job.get("name"), "step": step.get("name"), "number": step.get("number")})
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    records = load_records(directory)
    failed = next((record for record in records if int(record.get("exit_status", 0)) != 0), None)
    if failed is None:
        failed = {"stage": "unknown", "exit_status": None, "raw_log": "No failed wrapped stage was recorded."}
        if github.get("failed_steps"):
            failed["stage"] = github["failed_steps"][0].get("step") or "unknown"
    github_text = ""
    github_log = directory / "github-actions.log"
    if github_log.exists():
        github_text = "GITHUB ACTIONS LOGS:\n" + github_log.read_text(encoding="utf-8", errors="replace")
    evidence = "\n\n".join(
        f"STAGE: {record.get('stage')}\nACTION: {record.get('action')}\nEXIT STATUS: {record.get('exit_status')}\n"
        f"STARTED: {record.get('started_at')}\nFINISHED: {record.get('finished_at')}\nRAW LOG:\n{record.get('raw_log', '')}"
        for record in records
    )
    if github_text:
        evidence += "\n\n" + github_text
    incident: dict[str, Any] = {"incident_id": f"{context()['run_id']}-{hashlib.sha256(evidence.encode()).hexdigest()[:12]}",
        "failure_stage": failed.get("stage"), "original_exit_status": failed.get("exit_status"),
        "evidence": evidence, "stages": records, "github_failed_steps": github.get("failed_steps", []), **context()}
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "incident.json"
    path.write_text(json.dumps(incident, indent=2), encoding="utf-8")
    (directory / "evidence.txt").write_text(evidence, encoding="utf-8")
    return path, incident


def _report_template(incident: dict[str, Any], note: str, files: list[Path]) -> str:
    return "\n".join([
        f"# Incident {incident['incident_id']}", f"\nFailure stage: {incident.get('failure_stage')}",
        f"Original exit status: {incident.get('original_exit_status')}",
        f"Commit SHA: {incident.get('commit_sha')}", f"Workflow run: {incident.get('workflow_run_url')}",
        "\n## Evidence and finding", note,
        "\n## Root cause or uncertainty", "The available evidence is insufficient to confirm a root cause; this is intentionally reported as uncertainty.",
        "\n## Impact", "The original pipeline failure remains authoritative; downstream impact requires human review.",
        "\n## Files examined", *([f"- {p.relative_to(ROOT)}" for p in files] or ["- No repository file was available."]),
        "\n## Minimal proposed fix", "No change is proposed until evidence justifies one.",
        "\n## Alternatives and risks", "Collect additional read-only diagnostics or retry only under the normal pipeline policy; do not mask the failure.",
        "\n## Tests and verification plan", "Reproduce or validate the exact failed action, then rerun the complete affected pipeline and verify the result.",
        "\n## Approval", "No approval is present. Any proposal must be reviewed by the protected GitHub environment for this exact incident, source commit, and diff.",
    ]) + "\n"


def investigate(incident_path: Path, report_path: Path) -> int:
    incident = json.loads(incident_path.read_text(encoding="utf-8"))
    evidence = incident.get("evidence", "")
    files = _relevant_files(evidence)
    context_text = "\n\n".join(f"--- {p.relative_to(ROOT)} ---\n{p.read_text(encoding='utf-8', errors='replace')[:6000]}" for p in files)
    prompt = f"""Investigate this CI/CD incident using only the evidence and read-only repository excerpts. Correlate the exact failed stage/action with logs. Separate confirmed findings from assumptions. Discover the cause; do not assume a fixed taxonomy. A code change is optional and must be minimal and justified. If evidence is insufficient, say so and request specific read-only diagnostics. Never claim recovery or verification that is not shown. Return a report with: failure stage, evidence, root cause or uncertainty, impact, files examined, minimal proposed fix if warranted, alternatives, risks, tests, verification plan. Incident JSON:\n{json.dumps(incident)}\nRepository excerpts:\n{context_text}"""
    note = "The investigator was not contacted. "
    endpoint = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
    try:
        request = urllib.request.Request(endpoint, data=json.dumps({"model": os.getenv("OLLAMA_MODEL", "llama3.2:3b"), "prompt": prompt, "stream": False}).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout= int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120"))) as response:
            payload = json.loads(response.read().decode())
        answer = payload.get("response")
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("Ollama response did not contain a non-empty response")
        if not has_required_report_sections(answer):
            raise ValueError("Ollama response omitted required incident report sections")
        report = answer.strip() + "\n\n## Approval\nNo approval is present. Any proposal must be reviewed by the protected GitHub environment for this exact incident, source commit, and diff.\n"
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
        report = _report_template(incident, note + f"Investigation unavailable: {error}", files)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="operation", required=True)
    run = sub.add_parser("run")
    run.add_argument("--stage", required=True)
    run.add_argument("--action")
    run.add_argument("command", nargs=argparse.REMAINDER)
    collect = sub.add_parser("collect")
    collect.add_argument("--report", required=True)
    args = parser.parse_args()
    if args.operation == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("run requires a command after --")
        return run_stage(args.stage, command, args.action)
    incident_path, _ = build_incident()
    return investigate(incident_path, Path(args.report))


if __name__ == "__main__":
    raise SystemExit(main())
