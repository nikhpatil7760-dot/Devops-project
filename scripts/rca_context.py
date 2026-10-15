"""Shared helpers for CI/CD root cause analysis.

Redaction, size-capped prioritized context, stage-specific evidence extraction,
read-only Git/GitHub/Kubernetes collectors, and the structured report schema.
Every collector is read-only and degrades to an empty or descriptive result
instead of raising, so RCA can never change or hide the pipeline result.
Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".local", ".venv", "venv", "__pycache__", ".pytest_cache", ".terraform", "node_modules"}
MAX_TEXT_FILE_BYTES = 256 * 1024


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def settings() -> dict[str, Any]:
    """All RCA tunables; every value can be overridden through the environment."""
    return {
        "ollama_url": os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate"),
        "ollama_model": os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
        # Sized for a CPU-only runner with a 3B model (~12k chars takes 2-3 minutes);
        # raise RCA_MAX_CONTEXT_CHARS and OLLAMA_NUM_CTX together on faster hardware.
        "ollama_timeout": env_int("OLLAMA_TIMEOUT_SECONDS", 300),
        "ollama_num_ctx": env_int("OLLAMA_NUM_CTX", 6144),
        "ollama_num_predict": env_int("OLLAMA_NUM_PREDICT", 1200),
        "max_context_chars": env_int("RCA_MAX_CONTEXT_CHARS", 12000),
        "log_tail_lines": env_int("RCA_LOG_TAIL_LINES", 80),
        "max_error_lines": env_int("RCA_MAX_ERROR_LINES", 60),
        "max_files": env_int("RCA_MAX_FILES", 8),
        "command_timeout": env_int("RCA_COMMAND_TIMEOUT_SECONDS", 20),
        "github_api_url": os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
        "k8s_mode": os.getenv("RCA_ENABLE_K8S", "auto").strip().lower(),
        "k8s_namespace": os.getenv("RCA_K8S_NAMESPACE", "flask-app"),
        "k8s_selector": os.getenv("RCA_K8S_SELECTOR", "app.kubernetes.io/name=flask-app"),
    }


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

REDACTED = "[REDACTED]"
_SECRET_WORDS = r"password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|authorization|client[_-]?secret|conn(?:ection)?[_-]?string"
_ENV_SECRET_NAME = re.compile(r"(?i)password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|client[_-]?secret|conn(?:ection)?[_-]?string")
_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"(?i)\b(bearer|basic)\s+(?!\[REDACTED)[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@"), r"\1" + REDACTED + "@"),
]
_KEY_VALUE = re.compile(
    r"(?i)(\b[\w.-]*(?:" + _SECRET_WORDS + r")[\w.-]*)(\"?'?\s*[:=]\s*\"?'?)([^\s\"',;]{3,})"
)


def _env_secret_values() -> list[str]:
    extra = {name.strip() for name in os.getenv("RCA_REDACT_ENV_NAMES", "").split(",") if name.strip()}
    values = {
        value
        for name, value in os.environ.items()
        if (_ENV_SECRET_NAME.search(name) or name in extra) and len(value) >= 6
    }
    return sorted(values, key=len, reverse=True)


def redact_count(text: str) -> tuple[str, int]:
    """Return text with secrets replaced, plus the number of replacements."""
    if not text:
        return text or "", 0
    count = 0
    for value in _env_secret_values():
        if value in text:
            count += text.count(value)
            text = text.replace(value, REDACTED)
    for pattern, replacement in _PATTERNS:
        text, replaced = pattern.subn(replacement, text)
        count += replaced

    def key_value(match: re.Match[str]) -> str:
        nonlocal count
        value = match.group(3)
        # Leave code references and CI expressions readable: os.getenv("X"), ${{ ... }}, $VAR.
        if "(" in value or value.startswith(("$", "[REDACTED", "<", "{")) or value.lower() in {"none", "null", "true", "false", "required"}:
            return match.group(0)
        count += 1
        return match.group(1) + match.group(2) + REDACTED

    text = _KEY_VALUE.sub(key_value, text)
    return text, count


def redact(text: str) -> str:
    return redact_count(text)[0]


def redact_data(value: Any) -> Any:
    """Redact every string inside nested dicts and lists."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_data(item) for key, item in value.items()}
    return value


# --------------------------------------------------------------------------
# Truncation and prioritized context
# --------------------------------------------------------------------------

def compact_text(text: str, limit: int) -> str:
    """Fit text into limit characters, keeping a little of the start and most of the end."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit < 80:
        return text[-limit:]
    keep = limit - 60
    head = keep // 4
    tail = keep - head
    marker = f"\n... [{len(text) - keep} chars omitted] ...\n"
    return text[:head] + marker + text[-tail:]


def tail_lines(text: str, count: int) -> str:
    return "\n".join(text.splitlines()[-count:]) if count > 0 else ""


def build_prompt_context(sections: list[tuple[str, str, float | None]], budget: int) -> tuple[str, bool]:
    """Assemble sections in priority order under a hard character budget.

    Each section gets at most share * budget characters (None means whatever is
    left).  Earlier sections are filled first, so error lines and the diff are
    never squeezed out by lower-priority repository excerpts.
    """
    parts: list[str] = []
    remaining = budget
    truncated = False
    for title, body, share in sections:
        body = (body or "").strip()
        if not body:
            continue
        header = f"### {title}\n"
        cap = remaining if share is None else min(int(budget * share), remaining)
        room = cap - len(header) - 2
        if room < 120:
            truncated = True
            continue
        text = compact_text(body, room)
        truncated = truncated or len(text) < len(body)
        parts.append(header + text)
        remaining -= len(header) + len(text) + 2
    return "\n\n".join(parts), truncated


# --------------------------------------------------------------------------
# Log evidence extraction
# --------------------------------------------------------------------------

ERROR_RE = re.compile(
    r"(?i)\w*error\b|\berr!|exception|traceback|\bfailed\b|failure|fatal|denied|unauthorized|forbidden|"
    r"not found|no such|no module named|command not found|is not recognized|cannot|can't|unable to|refused|timed? ?out|exit (?:code|status)|panic|"
    r"oomkilled|crashloopbackoff|imagepullbackoff|errimagepull|back-off|assert|^E\s{2,}|##\[error\]"
)
_NOISE_RE = re.compile(r"(?i)\b0 (?:failed|errors?)\b|errors?=0\b|--fail|ignore[-_]?errors?|continue-on-error")
PYTEST_RE = re.compile(
    r"^(?:FAILED|ERROR) \S|^E\s{2,}\S|^[\w./\\-]+\.py:\d+:\s|^=+ .*\b(?:failed|errors?)\b.* =+$|^_{3,} .+ _{3,}$|"
    r"ModuleNotFoundError|ImportError while importing|collected \d+ items? / \d+ errors?"
)
DOCKER_RE = re.compile(
    r"(?i)failed to solve|executor failed running|failed to compute cache key|^#\d+ .*\berror\b|^ERROR(?:\s*\[|:)|"
    r"dockerfile:\d+|did not complete successfully|returned a non-zero code|failed to (?:build|fetch|read dockerfile)|"
    r"no space left on device|cannot connect to the docker daemon|error during connect"
)
REGISTRY_RE = re.compile(
    r"(?i)pull access denied|manifest unknown|manifest for \S+ not found|repository does not exist|name unknown|"
    r"\bunauthorized\b|authentication required|denied:|toomanyrequests|rate limit|no basic auth credentials|x509|"
    r"tls handshake|i/o timeout|no such host|errimagepull|imagepullbackoff|failed to pull|failed to resolve reference|"
    r"requested access to the resource is denied|image \S+ not (?:present|found)"
)


def _strip_line(line: str) -> str:
    # GitHub log lines start with an ISO timestamp; drop it so identical lines dedupe.
    return re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s", "", line.rstrip())


def error_lines(text: str, limit: int = 60) -> list[str]:
    """Exact error-looking lines (deduplicated, latest kept), prefixed by their line number."""
    found: dict[str, int] = {}
    for number, line in enumerate((text or "").splitlines(), start=1):
        stripped = _strip_line(line)
        if stripped.strip() and ERROR_RE.search(stripped) and not _NOISE_RE.search(stripped):
            found.pop(stripped, None)
            found[stripped] = number
    items = list(found.items())[-limit:] if limit > 0 else []
    return [f"L{number}: {line[:500]}" for line, number in items]


def matching_lines(pattern: re.Pattern[str], text: str, limit: int = 30) -> list[str]:
    found: dict[str, None] = {}
    for line in (text or "").splitlines():
        stripped = _strip_line(line)
        if stripped.strip() and pattern.search(stripped):
            found.pop(stripped, None)
            found[stripped[:500]] = None
    return list(found)[-limit:]


def iter_files(root: Path, skip: set[str]) -> Iterator[Path]:
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in skip]
        for name in filenames:
            yield Path(directory) / name


def is_text_file(path: Path) -> bool:
    try:
        if path.stat().st_size > MAX_TEXT_FILE_BYTES:
            return False
        with path.open("rb") as handle:
            return b"\0" not in handle.read(4096)
    except OSError:
        return False


def junit_failures(root: Path = ROOT, since: float | None = None, limit: int = 20) -> list[str]:
    """Failures from JUnit XML reports written during this run (e.g. pytest --junitxml)."""
    results: list[str] = []
    skip = SKIP_DIRS - {".local"}
    for path in iter_files(root, skip):
        name = path.name.lower()
        if not name.endswith(".xml") or not ("junit" in name or "test-results" in name or name.startswith("test-")):
            continue
        try:
            if since is not None and path.stat().st_mtime < since:
                continue
            tree = ET.parse(path)
        except (ET.ParseError, OSError):
            continue
        for case in tree.iter("testcase"):
            for tag in ("failure", "error"):
                node = case.find(tag)
                if node is None:
                    continue
                message = (node.get("message") or (node.text or "").strip().splitlines()[-1:] or [""])
                message = message if isinstance(message, str) else message[0]
                results.append(f"{case.get('classname', '')}::{case.get('name', '')} [{tag}] {message}"[:500])
    return results[:limit]


def _timestamp(value: str | None) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp() if value else None
    except ValueError:
        return None


def stage_evidence(log: str, started_at: str | None = None, limit: int = 30) -> dict[str, list[str]]:
    """Stage-specific evidence: pytest/JUnit failures, Docker build errors, registry errors."""
    evidence = {
        "pytest": matching_lines(PYTEST_RE, log, limit),
        "junit": junit_failures(since=_timestamp(started_at)),
        "docker": matching_lines(DOCKER_RE, log, limit),
        "registry": matching_lines(REGISTRY_RE, log, limit),
    }
    return {key: value for key, value in evidence.items() if value}


def format_stage_evidence(evidence: dict[str, list[str]]) -> str:
    titles = {"pytest": "Pytest failures", "junit": "JUnit failures", "docker": "Docker build errors", "registry": "Registry / image errors"}
    return "\n\n".join(f"{titles.get(key, key)}:\n" + "\n".join(lines) for key, lines in evidence.items() if lines)


# --------------------------------------------------------------------------
# Repository context
# --------------------------------------------------------------------------

FILE_REF_RE = re.compile(
    r"""(?P<path>(?:[A-Za-z]:)?(?:[\w.-]+[\\/])*(?:[\w.-]+\.(?:py|ya?ml|js|ts|json|toml|cfg|ini|txt|tf|sh|ps1|html|css)|Dockerfile))"""
    r"""(?:"?,\s*line\s+|:)(?P<line>\d+)"""
)


def resolve_repo_path(raw: str, root: Path = ROOT) -> str | None:
    """Map a path from a log (absolute, relative, Windows or POSIX) to a repo-relative path."""
    if "site-packages" in raw or ".venv" in raw:
        return None
    parts = [part for part in re.split(r"[\\/]+", raw) if part and not part.endswith(":")]
    for start in range(len(parts)):
        candidate = root.joinpath(*parts[start:])
        try:
            if candidate.is_file():
                return candidate.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
    return None


def referenced_files(text: str, root: Path = ROOT, limit: int = 8) -> list[tuple[str, int]]:
    """file:line references found in logs, latest first, resolved inside the repository."""
    found: dict[tuple[str, int], None] = {}
    for match in FILE_REF_RE.finditer(text or ""):
        relative = resolve_repo_path(match.group("path"), root)
        if relative:
            key = (relative, int(match.group("line")))
            found.pop(key, None)
            found[key] = None
    return list(reversed(list(found)))[:limit]


def file_snippet(relative: str, line: int, radius: int = 10, root: Path = ROOT) -> str:
    try:
        lines = (root / relative).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    start, end = max(1, line - radius), min(len(lines), line + radius)
    body = "\n".join(f"{number:>5}{'>' if number == line else ' '} {lines[number - 1]}" for number in range(start, end + 1))
    return f"--- {relative}:{line} ---\n{body}"


STAGE_CONFIG = [
    (re.compile(r"(?i)test|pytest"), ["app/requirements.txt", "app/requirements-dev.txt", "pytest.ini", "pyproject.toml", "setup.cfg"]),
    (re.compile(r"(?i)install|pip|depend|python"), ["app/requirements.txt", "app/requirements-dev.txt"]),
    (re.compile(r"(?i)docker|image|build"), ["Dockerfile", ".dockerignore", "app/requirements.txt"]),
    (re.compile(r"(?i)kube|kind|apply|deploy|rollout|namespace|service|cluster|context|health"),
     ["kubernetes/deployment.yaml", "kubernetes/service.yaml", "kubernetes/namespace.yaml", "kubernetes/kind-config.yaml"]),
]


def workflow_file(root: Path = ROOT) -> str | None:
    ref = os.getenv("GITHUB_WORKFLOW_REF", "")
    match = re.search(r"(\.github/workflows/[^@]+)", ref)
    candidates = [match.group(1)] if match else []
    candidates.append(".github/workflows/cicd.yml")
    return next((path for path in candidates if (root / path).is_file()), None)


def relevant_config(stage: str, command: str, root: Path = ROOT) -> list[str]:
    """Config snippets for the failed stage: the stage's workflow step plus stage-type config."""
    snippets: list[str] = []
    workflow = workflow_file(root)
    if workflow:
        lines = (root / workflow).read_text(encoding="utf-8", errors="replace").splitlines()
        hit = next((index for index, line in enumerate(lines, start=1) if f'"{stage}"' in line or f"name: {stage}" in line), None)
        if hit:
            snippets.append(file_snippet(workflow, hit, radius=4, root=root))
    probe = f"{stage} {command}"
    extra = [path.strip() for path in os.getenv("RCA_CONFIG_FILES", "").split(",") if path.strip()]
    paths = list(dict.fromkeys(extra + [path for pattern, files in STAGE_CONFIG if pattern.search(probe) for path in files]))
    for relative in paths:
        path = root / relative
        if path.is_file() and is_text_file(path):
            snippets.append(f"--- {relative} ---\n{compact_text(path.read_text(encoding='utf-8', errors='replace'), 2500)}")
    return snippets


# --------------------------------------------------------------------------
# Git / GitHub (read-only)
# --------------------------------------------------------------------------

def run_command(arguments: list[str], timeout: int = 20, cwd: Path = ROOT) -> str | None:
    try:
        result = subprocess.run(arguments, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def github_get(path: str, config: dict[str, Any]) -> Any:
    repository = os.getenv("GITHUB_REPOSITORY")
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not repository or not token:
        return None
    request = urllib.request.Request(
        f"{config['github_api_url']}/repos/{repository}/{path.lstrip('/')}",
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"},
    )
    try:
        with urllib.request.urlopen(request, timeout=config["command_timeout"]) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError, urllib.error.URLError):
        return None


def head_sha(sha: str | None) -> str | None:
    if sha and sha != "unknown":
        return sha
    output = run_command(["git", "rev-parse", "HEAD"])
    return output.strip() if output else None


def commit_info(sha: str | None, config: dict[str, Any]) -> dict[str, Any]:
    sha = head_sha(sha)
    info: dict[str, Any] = {"sha": sha}
    if not sha:
        return info
    data = github_get(f"commits/{sha}", config)
    if isinstance(data, dict) and data.get("commit"):
        commit = data["commit"]
        info.update(message=commit.get("message"), author=(commit.get("author") or {}).get("name"),
                    date=(commit.get("author") or {}).get("date"), url=data.get("html_url"), source="github")
        return info
    output = run_command(["git", "log", "-1", "--format=%an%n%aI%n%B", sha], timeout=config["command_timeout"])
    if output:
        author, date, *message = output.split("\n")
        info.update(author=author, date=date, message="\n".join(message).strip(), source="git")
    return info


def last_successful_run(head: str | None, config: dict[str, Any]) -> dict[str, Any] | None:
    """The newest successful run of this workflow on this branch for a different commit."""
    if os.getenv("RCA_BASE_SHA"):
        return {"sha": os.environ["RCA_BASE_SHA"], "source": "RCA_BASE_SHA"}
    run_id = os.getenv("GITHUB_RUN_ID")
    run = github_get(f"actions/runs/{run_id}", config) if run_id else None
    if not isinstance(run, dict) or not run.get("workflow_id"):
        return None
    query = f"actions/workflows/{run['workflow_id']}/runs?status=success&per_page=20"
    if run.get("head_branch"):
        query += "&branch=" + urllib.parse.quote(run["head_branch"])
    runs = github_get(query, config)
    for candidate in (runs or {}).get("workflow_runs", []) if isinstance(runs, dict) else []:
        if candidate.get("head_sha") and candidate.get("head_sha") != head:
            return {"sha": candidate["head_sha"], "run_id": candidate.get("id"), "run_url": candidate.get("html_url"), "source": "github"}
    return None


def _api_files(files: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    listed = [{"path": item.get("filename"), "status": item.get("status"), "additions": item.get("additions"), "deletions": item.get("deletions")} for item in files]
    diff = "\n".join(
        f"diff --git a/{item.get('filename')} b/{item.get('filename')}\n{item.get('patch') or '(binary or too large; patch not provided)'}"
        for item in files
    )
    return listed, diff


def _git_name_status(base: str, head: str, timeout: int) -> list[dict[str, Any]]:
    output = run_command(["git", "diff", "--name-status", f"{base}..{head}"], timeout=timeout) or ""
    return [{"path": parts[-1], "status": parts[0]} for parts in (line.split("\t") for line in output.splitlines()) if len(parts) >= 2]


def collect_changes(sha: str | None, config: dict[str, Any]) -> dict[str, Any]:
    """Diff since the last successful build, or the triggering commit when no base is known."""
    head = head_sha(sha)
    result: dict[str, Any] = {"head": head, "base": None, "source": None, "files": [], "diff": ""}
    if not head:
        return result
    timeout = config["command_timeout"]
    base = last_successful_run(head, config)
    if base:
        result["base"] = base
        data = github_get(f"compare/{base['sha']}...{head}", config)
        if isinstance(data, dict) and isinstance(data.get("files"), list):
            result["files"], result["diff"] = _api_files(data["files"])
            result["source"] = "github-compare"
            return result
        diff = run_command(["git", "diff", "--no-color", f"{base['sha']}..{head}"], timeout=timeout)
        if diff is not None:
            result.update(source="git-diff", diff=diff, files=_git_name_status(base["sha"], head, timeout))
            return result
    data = github_get(f"commits/{head}", config)
    if isinstance(data, dict) and isinstance(data.get("files"), list):
        result["files"], result["diff"] = _api_files(data["files"])
        result["source"] = "github-commit"
        return result
    # A shallow clone has no parent, so `git show` would list the whole tree as added.
    if (run_command(["git", "rev-parse", "--is-shallow-repository"]) or "").strip() == "false":
        diff = run_command(["git", "show", "--no-color", "--format=", head], timeout=timeout)
        if diff is not None:
            result.update(source="git-commit", diff=diff, files=_git_name_status(f"{head}~1", head, timeout))
    return result


# --------------------------------------------------------------------------
# Kubernetes (read-only)
# --------------------------------------------------------------------------

_K8S_COMMAND_WORDS = ("kubectl", "kind ", "helm", "deployment_check", "rollout")
_K8S_LOG_RE = re.compile(r"(?i)crashloopbackoff|imagepullbackoff|errimagepull|oomkilled|rollout status|readiness probe|liveness probe")


def wants_kubernetes(stage: str, command: str, log: str, config: dict[str, Any]) -> bool:
    mode = config["k8s_mode"]
    if mode in {"0", "false", "never", "off", "no"}:
        return False
    if mode in {"1", "true", "always", "on", "yes"}:
        return True
    probe = f"{stage} {command}".lower()
    return any(word in probe for word in _K8S_COMMAND_WORDS) or bool(_K8S_LOG_RE.search(log or ""))


def collect_kubernetes(config: dict[str, Any]) -> str:
    """Events, pod describe, current and --previous logs for the application pods."""
    if not shutil.which("kubectl"):
        return "kubectl is not available on the runner; Kubernetes evidence was not collected."
    namespace, selector, tail = config["k8s_namespace"], config["k8s_selector"], str(config["log_tail_lines"])

    def kubectl(*arguments: str) -> str:
        try:
            result = subprocess.run(["kubectl", *arguments], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=config["command_timeout"], check=False)
        except (OSError, subprocess.SubprocessError) as error:
            return f"Command failed: {error}"
        output = (result.stdout or "").strip()
        if result.returncode == 0:
            return output or "No output."
        return f"Command failed ({result.returncode}): {(result.stderr or output).strip()}"

    sections = [
        f"$ kubectl config current-context\n{kubectl('config', 'current-context')}",
        f"$ kubectl get deployment -n {namespace} -o wide\n{kubectl('get', 'deployment', '-n', namespace, '-o', 'wide')}",
        f"$ kubectl get pods -n {namespace} -l {selector} -o wide\n{kubectl('get', 'pods', '-n', namespace, '-l', selector, '-o', 'wide')}",
        f"$ kubectl get events -n {namespace} --sort-by=.lastTimestamp\n"
        + tail_lines(kubectl("get", "events", "-n", namespace, "--sort-by=.lastTimestamp"), 40),
    ]
    names = kubectl("get", "pods", "-n", namespace, "-l", selector, "-o", "jsonpath={.items[*].metadata.name}")
    if not names.startswith("Command failed") and names != "No output.":
        for pod in names.split()[:3]:
            sections.append(f"$ kubectl describe pod {pod}\n" + compact_text(kubectl("describe", "pod", pod, "-n", namespace), 3000))
            sections.append(f"$ kubectl logs {pod} --all-containers --tail={tail}\n"
                            + kubectl("logs", pod, "-n", namespace, "--all-containers=true", f"--tail={tail}"))
            sections.append(f"$ kubectl logs {pod} --all-containers --previous --tail={tail}\n"
                            + kubectl("logs", pod, "-n", namespace, "--all-containers=true", "--previous", f"--tail={tail}"))
    return "\n\n".join(sections)


# --------------------------------------------------------------------------
# Structured AI report
# --------------------------------------------------------------------------

CONFIDENCE_LEVELS = ("high", "medium", "low", "insufficient_data")
REPORT_SCHEMA_EXAMPLE = """{
  "summary": "one or two plain sentences",
  "failed_stage": "name of the failed stage",
  "error_lines": ["an error line copied exactly from the evidence"],
  "root_cause": {
    "description": "the cause, or 'Insufficient data' when the evidence does not show it",
    "evidence": [{"source": "path/to/file:LINE or the log name", "detail": "verbatim quote from the evidence"}],
    "confidence": "high | medium | low | insufficient_data"
  },
  "other_possible_causes": [{"description": "alternative cause", "confidence": "medium | low"}],
  "fix_steps": [{"description": "concrete step", "diff": "unified diff when a file must change, otherwise empty string"}],
  "verification": ["command or check that proves the fix works"],
  "prevention": ["how to stop this class of failure recurring"],
  "insufficient_data": false,
  "missing_data": ["what extra evidence is needed, when insufficient_data is true"]
}"""


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else json.dumps(value)


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [text for text in (_text(item) for item in items) if text]


def _confidence(value: Any, default: str = "low") -> str:
    normalized = _text(value).lower().replace(" ", "_").replace("-", "_")
    return normalized if normalized in CONFIDENCE_LEVELS else default


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^L\d+:\s*", "", text.strip())).strip().strip("`\"'").lower()


def make_grounding_check(context: str):
    """Return a predicate telling whether a quote really appears in the evidence."""
    haystack = _normalize(context)
    return lambda quote: len(_normalize(quote)) >= 6 and _normalize(quote) in haystack


def parse_ai_report(answer: str, context: str) -> dict[str, Any]:
    """Validate and normalize the model's JSON report; raise ValueError when unusable.

    Error lines that do not appear in the evidence are dropped, and confidence is
    downgraded when none of the cited evidence can be found in what was sent.
    """
    text = (answer or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI response did not contain a JSON object")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("AI response JSON was not an object")
    grounded = make_grounding_check(context)

    root = data.get("root_cause")
    root = root if isinstance(root, dict) else {"description": _text(root)}
    evidence = []
    for item in root.get("evidence") or []:
        source, detail = (_text(item.get("source")), _text(item.get("detail"))) if isinstance(item, dict) else ("", _text(item))
        if source or detail:
            path = source.split(":")[0]
            evidence.append({"source": source, "detail": detail, "verified": grounded(detail) or bool(path and grounded(path))})

    insufficient = data.get("insufficient_data") is True
    confidence = _confidence(root.get("confidence"), "insufficient_data" if insufficient else "low")
    if confidence in {"high", "medium"} and not any(item["verified"] for item in evidence):
        confidence = "low"
    if confidence == "insufficient_data":
        insufficient = True

    report = {
        "summary": _text(data.get("summary")),
        "failed_stage": _text(data.get("failed_stage")),
        "error_lines": [line for line in _text_list(data.get("error_lines")) if grounded(line)],
        "root_cause": {"description": _text(root.get("description")), "evidence": evidence, "confidence": confidence},
        "other_possible_causes": [
            {"description": _text(item.get("description")), "confidence": _confidence(item.get("confidence"))}
            if isinstance(item, dict) else {"description": _text(item), "confidence": "low"}
            for item in data.get("other_possible_causes") or [] if item
        ],
        "fix_steps": [
            {"description": _text(item.get("description")), "diff": _text(item.get("diff"))}
            if isinstance(item, dict) else {"description": _text(item), "diff": ""}
            for item in data.get("fix_steps") or [] if item
        ],
        "verification": _text_list(data.get("verification")),
        "prevention": _text_list(data.get("prevention")),
        "insufficient_data": insufficient,
        "missing_data": _text_list(data.get("missing_data")),
    }
    if not report["summary"] or not report["root_cause"]["description"]:
        raise ValueError("AI response omitted the summary or root cause")
    for step in report["fix_steps"]:
        if step["diff"] and diff_repeats_evidence(step["diff"], context):
            step["warning"] = ("This diff repeats a change already present in the evidence diff in the same direction; "
                               "it likely re-applies the breaking change instead of fixing it. Review before use.")
    return report


def _changed_lines(diff: str, sign: str) -> set[str]:
    return {line[1:].strip() for line in diff.splitlines()
            if line.startswith(sign) and not line.startswith(sign * 3) and line[1:].strip()}


def diff_repeats_evidence(fix_diff: str, context: str) -> bool:
    """True when every added/removed line of a proposed fix matches the evidence diff unchanged.

    Small models often echo the diff since the last successful build as the fix,
    which would re-apply the failure rather than correct it.
    """
    added, removed = _changed_lines(fix_diff, "+"), _changed_lines(fix_diff, "-")
    if not added and not removed:
        return False
    return added <= _changed_lines(context, "+") and removed <= _changed_lines(context, "-")


def fallback_analysis(stage: str, exit_code: Any, errors: list[str], evidence: dict[str, list[str]], action: str, reason: str) -> dict[str, Any]:
    """Deterministic report used when the AI is unavailable or its answer is unusable."""
    hints = []
    if evidence.get("pytest") or evidence.get("junit"):
        hints.append("One or more tests failed or could not be collected; see the pytest/JUnit failures.")
    if evidence.get("docker"):
        hints.append("The Docker build failed; see the Docker build errors.")
    if evidence.get("registry"):
        hints.append("An image or registry problem (missing image, credentials, rate limit or network); see the registry errors.")
    return {
        "summary": f"Stage '{stage}' failed with exit code {exit_code}. AI analysis was unavailable ({reason}), so no root cause is asserted.",
        "failed_stage": stage,
        "error_lines": errors[-15:],
        "root_cause": {
            "description": "Insufficient data: the available evidence is insufficient to confirm a root cause; this is intentionally reported as uncertainty.",
            "evidence": [{"source": f"log of stage '{stage}'", "detail": line, "verified": True} for line in errors[-3:]],
            "confidence": "insufficient_data",
        },
        "other_possible_causes": [{"description": hint, "confidence": "low"} for hint in hints],
        "fix_steps": [],
        "verification": [f"Re-run the failed action: `{action}`" if action else "Re-run the failed stage.",
                         "Re-run the complete pipeline and confirm every stage passes."],
        "prevention": [],
        "insufficient_data": True,
        "missing_data": [f"AI root cause analysis ({reason})"],
    }


def _fence(body: str, language: str = "text") -> str:
    fence = "~~~~" if "```" in body else "```"
    return f"{fence}{language}\n{body.rstrip()}\n{fence}"


def render_markdown(report: dict[str, Any]) -> str:
    """Human-readable report; keeps the headings existing consumers already read."""
    root = report.get("root_cause", {})
    analysis = report.get("analysis", {})
    commit = report.get("commit") or {}
    changes = report.get("changes") or {}
    source = (f"Ollama `{analysis.get('model')}`" if analysis.get("source") == "ollama"
              else f"Deterministic fallback ({analysis.get('fallback_reason')})")
    lines = [
        f"# Incident {report.get('incident_id')}",
        "",
        f"Failure stage: {report.get('failed_stage')}",
        f"Original exit status: {report.get('exit_code')}",
        f"Commit SHA: {report.get('commit_sha')}",
        f"Workflow run: {report.get('workflow_run_url')}",
        f"Analysis: {source}",
        "",
        "## Summary",
        report.get("summary") or "Insufficient data.",
        "",
        "## Failed stage and error lines",
        f"Stage `{report.get('failed_stage')}` exited with code {report.get('exit_code')}."
        + (f" Action: `{report.get('failed_action')}`" if report.get("failed_action") else ""),
        _fence("\n".join(report.get("error_lines") or ["No error lines were found in the captured log."])),
        "",
        "## Root cause or uncertainty",
        f"**Confidence:** {root.get('confidence', 'insufficient_data')}",
        "",
        root.get("description") or "Insufficient data.",
    ]
    if root.get("evidence"):
        lines += ["", "Evidence:"]
        lines += [f"- `{item.get('source') or 'evidence'}`: {item.get('detail')}" + ("" if item.get("verified") else " _(not found in collected evidence)_")
                  for item in root["evidence"]]
    lines += ["", "## Other possible causes"]
    lines += [f"- {item['description']} ({item['confidence']})" for item in report.get("other_possible_causes") or []] or ["- None identified."]
    lines += ["", "## Changes since last successful build"]
    base = changes.get("base") or {}
    if changes.get("source"):
        lines.append(f"Base: {base.get('sha', 'triggering commit only')} ({changes.get('source')}); head: {changes.get('head')}")
        lines += [f"- {item.get('status')} {item.get('path')}" for item in (changes.get("files") or [])[:40]] or ["- No changed files listed."]
    else:
        lines.append("Change information was not available.")
    if commit.get("message"):
        lines += ["", f"Commit: {commit.get('message', '').splitlines()[0]} ({commit.get('author')})"]
    lines += ["", "## Impact", "The original pipeline failure remains authoritative; later stages were skipped and the run stays failed."]
    lines += ["", "## Files examined"]
    lines += [f"- {path}" for path in report.get("files_examined") or []] or ["- No repository file was available."]
    lines += ["", "## Minimal proposed fix"]
    if report.get("fix_steps"):
        for number, step in enumerate(report["fix_steps"], start=1):
            lines.append(f"{number}. {step.get('description')}")
            if step.get("warning"):
                lines.append(f"   > **Warning:** {step['warning']}")
            if step.get("diff"):
                lines.append(_fence(step["diff"], "diff"))
    else:
        lines.append("No change is proposed until evidence justifies one.")
    lines += ["", "## Tests and verification plan"]
    lines += [f"- {item}" for item in report.get("verification") or []] or ["- Re-run the failed stage, then the complete pipeline."]
    lines += ["", "## Prevention"]
    lines += [f"- {item}" for item in report.get("prevention") or []] or ["- None proposed."]
    if report.get("insufficient_data"):
        lines += ["", "## Missing data", "Insufficient data to confirm a root cause."]
        lines += [f"- {item}" for item in report.get("missing_data") or []]
    lines += ["", "## Approval",
              "No approval is present. Any proposal must be reviewed by the protected GitHub environment for this exact incident, source commit, and diff."]
    return "\n".join(lines) + "\n"
