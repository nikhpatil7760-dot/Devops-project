#!/usr/bin/env python3
"""Ask Ollama for a reviewable remediation patch without applying it."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


def load_evidence(directory: Path) -> str:
    files = sorted(directory.rglob("*.txt"), key=lambda path: path.stat().st_mtime)
    if not files:
        raise FileNotFoundError(f"No incident evidence files found in {directory}")
    return "\n\n".join(
        f"--- {path.name} ---\n{path.read_text(encoding='utf-8')}" for path in files
    )


def clean_patch(response: str) -> str:
    patch = response.strip()
    fence = chr(96) * 3
    if patch.startswith(fence):
        lines = patch.splitlines()
        if lines and lines[0].startswith(fence):
            lines = lines[1:]
        if lines and lines[-1].strip() == fence:
            lines = lines[:-1]
        patch = "\n".join(lines).strip()
    marker = patch.find("diff --git ")
    if marker > 0:
        patch = patch[marker:]
    return patch


def validate_patch(patch: str) -> None:
    if not patch.startswith("diff --git "):
        raise ValueError("Ollama did not return a unified git diff.")
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) != 4:
            raise ValueError("The remediation diff has an invalid file header.")
        for path in parts[2:]:
            relative = path[2:] if path[:2] in {"a/", "b/"} else path
            if relative.startswith("/") or relative.startswith("..") or "/../" in relative:
                raise ValueError("The remediation diff contains an unsafe file path.")
    check = subprocess.run(
        ["git", "apply", "--check", "--recount", "-"],
        input=patch,
        text=True,
        capture_output=True,
        check=False,
    )
    if check.returncode:
        detail = (check.stderr or check.stdout or "git apply check failed").strip()
        raise ValueError(f"Generated patch cannot be applied cleanly: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--patch-output", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()

    try:
        evidence = load_evidence(args.evidence_dir)
        prompt = f"""You are a senior software maintainer preparing a remediation proposal.
Review only the incident evidence below and the checked-out repository. Determine the smallest safe code or configuration change that addresses the evidenced failure.
Do not invent a cause. Do not change deployment recovery behavior unless the evidence requires it. Preserve existing tests and security controls.
Return only a unified git diff that applies to the checked-out repository. Do not use markdown fences, explanations, shell commands, secrets, or generated files. If the evidence is insufficient to justify a safe code change, return an empty response.

INCIDENT EVIDENCE:
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
                    "options": {"num_ctx": 8192, "num_predict": 1024, "temperature": 0},
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=args.timeout_seconds) as response:
            patch = clean_patch(json.load(response).get("response", ""))
        validate_patch(patch)
        args.patch_output.parent.mkdir(parents=True, exist_ok=True)
        args.patch_output.write_text(patch + "\n", encoding="utf-8")
        print(f"Reviewable remediation patch written to {args.patch_output}")
        return 0
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as error:
        print(f"Remediation patch was not generated: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())