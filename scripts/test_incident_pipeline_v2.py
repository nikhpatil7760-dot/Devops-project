import importlib.util
import json
from pathlib import Path

import pytest


def load(name):
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


incident_pipeline = load("incident_pipeline")
proposal_binding = load("proposal_binding")


def test_unexpected_command_has_generic_stage_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(incident_pipeline, "INCIDENT_DIR", tmp_path / "incidents")
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    status = incident_pipeline.run_stage("Future unclassified stage", ["cmd", "/c", "exit", "7"])
    assert status == 7
    record = json.loads(next((tmp_path / "incidents" / "stages").glob("*.json")).read_text())
    assert record["stage"] == "Future unclassified stage" and record["exit_status"] == 7
    assert "raw_log" in record and record["commit_sha"] == "abc123"


def test_collection_preserves_failure_when_ollama_is_unavailable(tmp_path, monkeypatch):
    directory = tmp_path / "incidents"
    monkeypatch.setattr(incident_pipeline, "INCIDENT_DIR", directory)
    incident_pipeline.run_stage("Unknown stage", ["cmd", "/c", "exit", "3"])
    incident_path, incident = incident_pipeline.build_incident(directory)
    assert incident["original_exit_status"] == 3
    monkeypatch.setattr(incident_pipeline.urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    report = tmp_path / "report.md"
    incident_pipeline.investigate(incident_path, report)
    assert "insufficient to confirm" in report.read_text().lower()
    assert json.loads(incident_path.read_text())["original_exit_status"] == 3


def test_malformed_ollama_response_is_uncertain(tmp_path, monkeypatch):
    directory = tmp_path / "incidents"
    monkeypatch.setattr(incident_pipeline, "INCIDENT_DIR", directory)
    incident_pipeline.run_stage("Tests", ["cmd", "/c", "exit", "1"])
    incident_path, _ = incident_pipeline.build_incident(directory)

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"response": ""}'

    monkeypatch.setattr(incident_pipeline.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    report = tmp_path / "report.md"
    incident_pipeline.investigate(incident_path, report)
    assert "insufficient to confirm" in report.read_text().lower()


def test_stale_or_changed_proposal_is_rejected(tmp_path):
    incident = tmp_path / "incident.json"
    patch = tmp_path / "proposal.patch"
    manifest = tmp_path / "manifest.json"
    incident.write_text(json.dumps({"incident_id": "i-1", "commit_sha": "sha-1"}))
    patch.write_text("diff --git a/app.py b/app.py\n")
    proposal_binding.create_manifest(incident, patch, manifest)
    proposal_binding.verify_manifest(incident, patch, manifest, "sha-1")
    with pytest.raises(ValueError):
        proposal_binding.verify_manifest(incident, patch, manifest, "sha-2")
    patch.write_text("unauthorized change")
    with pytest.raises(ValueError):
        proposal_binding.verify_manifest(incident, patch, manifest, "sha-1")
