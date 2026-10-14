"""Bind and revalidate remediation proposals against immutable identities."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_manifest(incident_path: Path, patch_path: Path, output: Path) -> dict[str, str]:
    incident = json.loads(incident_path.read_text(encoding="utf-8"))
    if any(not incident.get(key) or incident[key] == "unknown" for key in ("incident_id", "commit_sha")):
        raise ValueError("incident is missing an immutable incident_id or commit_sha")
    manifest = {
        "incident_id": incident["incident_id"],
        "source_sha": incident["commit_sha"],
        "patch_sha256": sha256(patch_path),
        "patch_file": patch_path.name,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify_manifest(incident_path: Path, patch_path: Path, manifest_path: Path, expected_head: str) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    incident = json.loads(incident_path.read_text(encoding="utf-8"))
    actual = {
        "incident_id": incident.get("incident_id"),
        "source_sha": incident.get("commit_sha"),
        "patch_sha256": sha256(patch_path),
    }
    for key, value in actual.items():
        if manifest.get(key) != value:
            raise ValueError(f"proposal binding mismatch for {key}")
    if expected_head != manifest["source_sha"]:
        raise ValueError("checked out source commit is not the incident source commit")


def approved_paths(patch: str) -> set[str]:
    paths: set[str] = set()
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = shlex.split(line)
        if len(parts) != 4:
            raise ValueError("proposal contains an invalid diff header")
        paths.update(path[2:] if path.startswith(("a/", "b/")) else path for path in parts[2:])
    return paths


def verify_worktree_diff(patch_path: Path) -> None:
    allowed = approved_paths(patch_path.read_text(encoding="utf-8"))
    changed = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"], capture_output=True, text=True, check=False
    )
    if changed.returncode:
        raise ValueError(changed.stderr.strip() or "could not inspect the worktree")
    actual = {line.strip().replace("\\", "/") for line in changed.stdout.splitlines() if line.strip()}
    if not actual.issubset(allowed):
        raise ValueError("worktree contains changes outside the approved patch")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="operation", required=True)
    make = sub.add_parser("create")
    make.add_argument("--incident", type=Path, required=True)
    make.add_argument("--patch", type=Path, required=True)
    make.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--incident", type=Path, required=True)
    verify.add_argument("--patch", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--head", required=True)
    worktree = sub.add_parser("verify-worktree")
    worktree.add_argument("--patch", type=Path, required=True)
    args = parser.parse_args()
    if args.operation == "create":
        create_manifest(args.incident, args.patch, args.output)
    elif args.operation == "verify":
        verify_manifest(args.incident, args.patch, args.manifest, args.head)
    else:
        verify_worktree_diff(args.patch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
