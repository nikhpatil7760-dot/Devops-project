"""Generic CI/CD failure evidence, investigation, and proposal primitives.

The stage runner is deliberately unaware of failure categories.  It records the
process that failed, while the investigator selects repository context from the
recorded evidence and asks Ollama for a report.  Neither component changes the
pipeline's original exit status.
"""

from __future__ import annotations

import argparse
import hashlib
import io
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rca_context as rca  # noqa: E402


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
    candidates = [p for p in rca.iter_files(ROOT, rca.SKIP_DIRS) if rca.is_text_file(p)]
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
    api = rca.settings()["github_api_url"]
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
    try:
        jobs_request = urllib.request.Request(f"{api}/repos/{repository}/actions/runs/{run_id}/jobs?per_page=100", headers=headers)
        with urllib.request.urlopen(jobs_request, timeout=30) as response:
            jobs = json.loads(response.read().decode())
        for job in jobs.get("jobs", []):
            for step in job.get("steps", []):
                if step.get("conclusion") == "failure":
                    result["failed_steps"].append({"job": job.get("name"), "step": step.get("name"), "number": step.get("number")})
        (directory / "github-actions.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        job_ids = [job.get("id") for job in jobs.get("jobs", []) if job.get("id")]
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
        # Evidence collection must never replace the original pipeline result.
        return result
    log_parts = []
    for job_id in job_ids:
        try:
            logs_request = urllib.request.Request(f"{api}/repos/{repository}/actions/jobs/{job_id}/logs", headers=headers)
            with urllib.request.urlopen(logs_request, timeout=60) as response:
                payload = response.read()
            try:
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    log_parts.extend(archive.read(name).decode("utf-8", errors="replace") for name in archive.namelist())
            except zipfile.BadZipFile:
                log_parts.append(payload.decode("utf-8", errors="replace"))
        except (OSError, ValueError, urllib.error.URLError):
            # One unavailable job log (e.g. the job still running) must not drop the others.
            continue
    if log_parts:
        (directory / "github-actions.log").write_text("\n\n".join(log_parts), encoding="utf-8")
        result["logs_available"] = True
    return result


def _safe(collector, default, *arguments):
    """Run one evidence collector; a broken collector must not stop the investigation."""
    try:
        return collector(*arguments)
    except Exception as error:  # noqa: BLE001 - RCA must never break the failure handler
        print(f"RCA collector {getattr(collector, '__name__', collector)} failed: {error}", file=sys.stderr)
        return default


def build_incident(directory: Path = INCIDENT_DIR) -> tuple[Path, dict[str, Any]]:
    directory.mkdir(parents=True, exist_ok=True)
    config = rca.settings()
    github = _safe(collect_github_actions_logs, {"failed_steps": []}, directory)
    jobs_file = directory / "github-actions-jobs.json"
    if jobs_file.exists():
        try:
            jobs_payload = json.loads(jobs_file.read_text(encoding="utf-8-sig"))
            for job in jobs_payload.get("jobs", []):
                for step in job.get("steps", []):
                    if step.get("conclusion") == "failure":
                        github.setdefault("failed_steps", []).append({"job": job.get("name"), "step": step.get("name"), "number": step.get("number")})
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            pass

    records = load_records(directory)
    failed = next((record for record in records if int(record.get("exit_status", 0)) != 0), None)
    github_raw = ""
    github_log = directory / "github-actions.log"
    if github_log.exists():
        github_raw = github_log.read_text(encoding="utf-8", errors="replace")
    if failed is None:
        failed = {"stage": "unknown", "exit_status": None, "raw_log": "No failed wrapped stage was recorded."}
        if github.get("failed_steps"):
            failed["stage"] = github["failed_steps"][0].get("step") or "unknown"
    # Without a wrapped stage (e.g. checkout failed) the GitHub job log is the failed log.
    failed_log = failed.get("raw_log", "") if failed.get("exit_status") is not None else (github_raw or failed.get("raw_log", ""))
    stage, action = str(failed.get("stage")), str(failed.get("action") or "")

    evidence = "\n\n".join(
        f"STAGE: {record.get('stage')}\nACTION: {record.get('action')}\nEXIT STATUS: {record.get('exit_status')}\n"
        f"STARTED: {record.get('started_at')}\nFINISHED: {record.get('finished_at')}\nRAW LOG:\n{record.get('raw_log', '')}"
        for record in records
    )
    if github_raw:
        evidence += "\n\nGITHUB ACTIONS LOGS:\n" + github_raw

    errors = rca.error_lines(failed_log, config["max_error_lines"])
    specific = _safe(rca.stage_evidence, {}, failed_log, failed.get("started_at"))
    commit = _safe(rca.commit_info, {"sha": context()["commit_sha"]}, context()["commit_sha"], config)
    changes = _safe(rca.collect_changes, {"files": [], "diff": ""}, context()["commit_sha"], config)
    kubernetes = ""
    if _safe(rca.wants_kubernetes, False, stage, action, failed_log, config):
        kubernetes = _safe(rca.collect_kubernetes, "", config)
    # Deployment checks save Kubernetes snapshots via ai-rca.py; reuse them as evidence.
    deployment_evidence = "\n\n".join(
        f"--- {path.name} ---\n{path.read_text(encoding='utf-8', errors='replace')}" for path in sorted(directory.glob("incident-*.txt"))
    )
    if specific:
        evidence += "\n\nSTAGE-SPECIFIC EVIDENCE:\n" + rca.format_stage_evidence(specific)
    if kubernetes:
        evidence += "\n\nKUBERNETES EVIDENCE:\n" + kubernetes
    evidence, redactions = rca.redact_count(evidence)

    diff = rca.redact(changes.pop("diff", "") or "")
    if diff:
        (directory / "rca-changes.diff").write_text(diff, encoding="utf-8")
        changes["diff_file"] = "rca-changes.diff"
    incident: dict[str, Any] = {"incident_id": f"{context()['run_id']}-{hashlib.sha256(evidence.encode()).hexdigest()[:12]}",
        "failure_stage": failed.get("stage"), "original_exit_status": failed.get("exit_status"),
        "evidence": evidence, "stages": rca.redact_data(records), "github_failed_steps": github.get("failed_steps", []), **context(),
        "failed_action": action, "failed_started_at": failed.get("started_at"),
        "error_lines": rca.redact_data(errors), "failed_log_tail": rca.redact(rca.tail_lines(failed_log, config["log_tail_lines"])),
        "stage_evidence": rca.redact_data(specific), "commit": rca.redact_data(commit), "changes": rca.redact_data(changes),
        "kubernetes_evidence": rca.redact(kubernetes), "deployment_evidence": rca.redact(deployment_evidence),
        "redactions": redactions}
    incident = rca.redact_data(incident)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "incident.json"
    path.write_text(json.dumps(incident, indent=2), encoding="utf-8")
    (directory / "evidence.txt").write_text(evidence, encoding="utf-8")
    return path, incident


def build_context(incident: dict[str, Any], directory: Path, config: dict[str, Any]) -> tuple[str, bool, list[str]]:
    """Size-capped, redacted prompt context; error lines and the diff come first."""
    stage, action = str(incident.get("failure_stage")), str(incident.get("failed_action") or "")
    errors = incident.get("error_lines") or []
    log_tail = incident.get("failed_log_tail") or ""
    changes = incident.get("changes") or {}
    commit = incident.get("commit") or {}
    diff = ""
    if changes.get("diff_file") and (directory / changes["diff_file"]).exists():
        diff = (directory / changes["diff_file"]).read_text(encoding="utf-8", errors="replace")

    examined: list[str] = []
    references = rca.referenced_files("\n".join(errors) + "\n" + log_tail, limit=config["max_files"])
    referenced = [rca.file_snippet(path, line) for path, line in references]
    examined += [path for path, _ in references]
    config_snippets = rca.relevant_config(stage, action)
    examined += [snippet.split("\n", 1)[0].strip("- ").split(":")[0] for snippet in config_snippets]
    changed_snippets = []
    for item in (changes.get("files") or [])[: config["max_files"]]:
        file_path = ROOT / str(item.get("path"))
        if file_path.is_file() and rca.is_text_file(file_path):
            changed_snippets.append(f"--- {item['path']} ---\n{rca.compact_text(file_path.read_text(encoding='utf-8', errors='replace'), 2500)}")
            examined.append(str(item["path"]))
    related = [p for p in _relevant_files(f"{stage}\n{action}\n" + "\n".join(errors), limit=config["max_files"])
               if p.relative_to(ROOT).as_posix() not in examined]
    related_text = "\n\n".join(f"--- {p.relative_to(ROOT).as_posix()} ---\n{p.read_text(encoding='utf-8', errors='replace')[:3000]}" for p in related)
    examined += [p.relative_to(ROOT).as_posix() for p in related]

    base = (changes.get("base") or {}).get("sha", "none known; showing the triggering commit")
    header = "\n".join([
        f"Failed stage: {stage}",
        f"Exit code: {incident.get('original_exit_status')}",
        f"Failed action: {action}",
        f"Workflow: {incident.get('workflow')} (event: {incident.get('event')}) {incident.get('workflow_run_url')}",
        f"Commit: {commit.get('sha')} by {commit.get('author')}: {(commit.get('message') or '').splitlines()[0] if commit.get('message') else ''}",
        f"Changes since last successful build: base {base}, {len(changes.get('files') or [])} files: "
        + ", ".join(f"{f.get('status')} {f.get('path')}" for f in (changes.get("files") or [])[:30]),
        "Stage results: " + ", ".join(f"{s.get('stage')}={s.get('exit_status')}" for s in incident.get("stages") or []),
        "Stages after the failed stage were skipped by the workflow.",
    ])
    tail_count = config["log_tail_lines"]
    sections = [
        ("Failure", header, 0.1),
        ("Error lines from the failed stage log (line numbers refer to that log)", "\n".join(errors), 0.22),
        ("Stage-specific evidence", rca.format_stage_evidence(incident.get("stage_evidence") or {}), 0.12),
        ("Git diff since last successful build", diff, 0.22),
        (f"Tail of failed stage log (last {tail_count} lines)", log_tail, 0.14),
        ("Kubernetes evidence", "\n\n".join(filter(None, [incident.get("kubernetes_evidence"), incident.get("deployment_evidence")])), 0.12),
        ("Files referenced by the errors (> marks the referenced line)", "\n\n".join(referenced), 0.12),
        ("Relevant configuration", "\n\n".join(config_snippets), 0.1),
        ("Changed files", "\n\n".join(changed_snippets), 0.08),
        ("Other related repository files", related_text, None),
    ]
    text, truncated = rca.build_prompt_context(sections, config["max_context_chars"])
    return rca.redact(text), truncated, list(dict.fromkeys(examined))


INVESTIGATION_PROMPT = """You are a senior CI/CD engineer performing root cause analysis of a failed pipeline run.
Use ONLY the evidence below. Rules:
- Copy error lines exactly as they appear in the evidence.
- Every root-cause claim must cite evidence: a verbatim log line, or a file path with line number (path:line) shown in the evidence.
- If the evidence does not show the cause, say "Insufficient data", set "insufficient_data" to true and confidence to "insufficient_data", and list what is missing in "missing_data". Do not guess.
- Confidence: "high" only when a quoted line directly shows the cause, "medium" when the evidence strongly suggests it, otherwise "low".
- Fix steps must be concrete. Include a unified diff when a file must change, otherwise an empty string. Never include secrets.
- The pipeline result is authoritative: do not claim anything was fixed, retried or rolled back.
Respond with a single JSON object with exactly these keys:
"""


def ask_ollama(prompt: str, config: dict[str, Any]) -> str:
    headers = {"Content-Type": "application/json"}
    if os.getenv("OLLAMA_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['OLLAMA_API_KEY']}"
    body = {"model": config["ollama_model"], "prompt": prompt, "stream": False, "format": "json",
            "options": {"num_ctx": config["ollama_num_ctx"], "num_predict": config["ollama_num_predict"], "temperature": 0}}
    request = urllib.request.Request(config["ollama_url"], data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=config["ollama_timeout"]) as response:
        payload = json.loads(response.read().decode())
    answer = payload.get("response")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Ollama response did not contain a non-empty response")
    return answer


def write_reports(incident: dict[str, Any], analysis: dict[str, Any], meta: dict[str, Any], files: list[str],
                  context_info: dict[str, Any], report_path: Path) -> None:
    """Write the Markdown report (existing path) and the JSON report next to it."""
    recorded_stage = incident.get("failure_stage")
    report: dict[str, Any] = {
        "schema_version": 1,
        **analysis,
        "incident_id": incident.get("incident_id"),
        "run_id": incident.get("run_id"),
        "workflow": incident.get("workflow"),
        "event": incident.get("event"),
        "workflow_run_url": incident.get("workflow_run_url"),
        "commit_sha": incident.get("commit_sha"),
        "failed_stage": recorded_stage if recorded_stage not in {None, "unknown"} else analysis.get("failed_stage") or "unknown",
        "exit_code": incident.get("original_exit_status"),
        "failed_action": incident.get("failed_action"),
        "generated_at": utc_now(),
        "analysis": meta,
        "commit": incident.get("commit"),
        "changes": incident.get("changes"),
        "files_examined": files,
        "context": context_info,
        "pipeline_result": "failed (the original result is authoritative; RCA never changes it)",
    }
    if not report.get("error_lines"):
        report["error_lines"] = (incident.get("error_lines") or [])[-10:]
    report = rca.redact_data(report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(rca.render_markdown(report), encoding="utf-8")
    report_path.with_suffix(".json").write_text(json.dumps(report, indent=2), encoding="utf-8")


def investigate(incident_path: Path, report_path: Path) -> int:
    config = rca.settings()
    incident = json.loads(incident_path.read_text(encoding="utf-8-sig"))
    meta: dict[str, Any] = {"source": "fallback", "model": None, "fallback_reason": None}
    context_text, truncated, files = "", False, []
    try:
        context_text, truncated, files = build_context(incident, incident_path.parent, config)
        (incident_path.parent / "rca-context.md").write_text(context_text, encoding="utf-8")
        prompt = INVESTIGATION_PROMPT + rca.REPORT_SCHEMA_EXAMPLE + "\n\nEVIDENCE:\n" + context_text
        analysis = rca.parse_ai_report(ask_ollama(prompt, config), context_text)
        meta.update(source="ollama", model=config["ollama_model"])
    except Exception as error:  # noqa: BLE001 - any failure falls back; the pipeline result is untouched
        reason = f"Investigation unavailable: {error}"
        print(reason, file=sys.stderr)
        analysis = rca.fallback_analysis(str(incident.get("failure_stage")), incident.get("original_exit_status"),
                                         incident.get("error_lines") or [], incident.get("stage_evidence") or {},
                                         str(incident.get("failed_action") or ""), reason)
        meta["fallback_reason"] = reason
    context_info = {"chars": len(context_text), "budget": config["max_context_chars"], "truncated": truncated,
                    "redactions": incident.get("redactions", 0)}
    write_reports(incident, analysis, meta, files, context_info, report_path)
    return 0


def collect(report_path: Path) -> int:
    """Build the incident and report; always exit 0 so RCA cannot alter the pipeline result."""
    try:
        incident_path, _ = build_incident()
    except Exception as error:  # noqa: BLE001
        print(f"Incident evidence collection failed: {error}", file=sys.stderr)
        INCIDENT_DIR.mkdir(parents=True, exist_ok=True)
        incident_path = INCIDENT_DIR / "incident.json"
        incident_path.write_text(json.dumps({"incident_id": f"{context()['run_id']}-collection-error", "failure_stage": "unknown",
            "original_exit_status": None, "evidence_status": f"incomplete; RCA collection error: {error}", "evidence": "", **context()},
            indent=2), encoding="utf-8")
    try:
        return investigate(incident_path, report_path)
    except Exception as error:  # noqa: BLE001
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(f"# Incident evidence\n\nInsufficient data: RCA report generation failed ({error}). "
                               "The original pipeline result is authoritative.\n", encoding="utf-8")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="operation", required=True)
    run = sub.add_parser("run")
    run.add_argument("--stage", required=True)
    run.add_argument("--action")
    run.add_argument("command", nargs=argparse.REMAINDER)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--report", required=True)
    args = parser.parse_args()
    if args.operation == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("run requires a command after --")
        return run_stage(args.stage, command, args.action)
    return collect(Path(args.report))


if __name__ == "__main__":
    raise SystemExit(main())
