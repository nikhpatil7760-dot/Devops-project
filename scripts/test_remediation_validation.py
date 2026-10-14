import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


def load(name):
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


binding = load("proposal_binding")
remediation = load("ollama-remediation")
ROOT = Path(__file__).resolve().parents[1]


def test_verify_cli_requires_patch_argument(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["proposal_binding.py", "verify", "--incident", "incident.json"])
    with pytest.raises(SystemExit):
        binding.main()


def test_missing_incident_evidence_is_rejected(tmp_path):
    with pytest.raises(FileNotFoundError):
        remediation.load_evidence(tmp_path)


def test_failed_ollama_analysis_does_not_create_a_proposal(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("stage failed\n", encoding="utf-8")
    output = tmp_path / "proposal.patch"
    monkeypatch.setenv("OLLAMA_URL", "http://unavailable.invalid")
    monkeypatch.setattr(remediation.urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(sys, "argv", ["ollama-remediation.py", "--evidence-dir", str(tmp_path), "--patch-output", str(output)])
    assert remediation.main() == 1
    assert not output.exists()


def test_invalid_and_stale_proposal_bindings_are_rejected(tmp_path):
    incident = tmp_path / "incident.json"
    patch = tmp_path / "proposal.patch"
    manifest = tmp_path / "proposal.json"
    incident.write_text(json.dumps({"incident_id": "incident-1", "commit_sha": "sha-1"}), encoding="utf-8")
    patch.write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")
    binding.create_manifest(incident, patch, manifest)
    binding.verify_manifest(incident, patch, manifest, "sha-1")
    with pytest.raises(ValueError):
        binding.verify_manifest(incident, patch, manifest, "sha-2")
    patch.write_text("changed approved artifact", encoding="utf-8")
    with pytest.raises(ValueError):
        binding.verify_manifest(incident, patch, manifest, "sha-1")


def test_changes_outside_approved_patch_are_rejected(tmp_path, monkeypatch):
    patch = tmp_path / "proposal.patch"
    patch.write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")
    result = subprocess.CompletedProcess(["git"], 0, "app.py\nsecrets.txt\n", "")
    monkeypatch.setattr(binding.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(ValueError, match="outside"):
        binding.verify_worktree_diff(patch)


def test_workflow_has_approval_binding_and_windows_safe_pr_creation():
    workflow = (ROOT / ".github" / "workflows" / "ollama-remediation.yml").read_text(encoding="utf-8")
    assert "environment: ollama-remediation-approval" in workflow
    assert "pull-requests: write" in workflow
    assert "GH_TOKEN: ${{ github.token }}" in workflow
    assert "shell: bash" not in workflow
    assert "apply_changes" not in workflow
    assert "--patch .local/ollama-remediation.patch" in workflow
    assert "verify-worktree --patch .local/ollama-remediation.patch" in workflow


def test_workflow_does_not_deploy_during_pull_request_validation():
    workflow = (ROOT / ".github" / "workflows" / "cicd.yml").read_text(encoding="utf-8")
    assert "github.event_name == 'push' && github.ref == 'refs/heads/main'" in workflow
    assert "pull_request" in workflow
    assert "kubectl apply" in workflow
