import importlib.util
import json
import sys
from pathlib import Path


def load(name):
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


remediation = load("ollama-remediation")


def test_successful_proposal_generation_writes_reviewable_patch(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("A future pipeline action failed with exit status 4.\n", encoding="utf-8")
    output = tmp_path / "proposal.patch"

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps({"response": "diff --git a/app/app.py b/app/app.py\n"}).encode()

    monkeypatch.setattr(remediation.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    monkeypatch.setattr(remediation, "validate_patch", lambda patch: None)
    monkeypatch.setattr(sys, "argv", ["ollama-remediation.py", "--evidence-dir", str(tmp_path), "--patch-output", str(output)])
    assert remediation.main() == 0
    assert output.read_text(encoding="utf-8").startswith("diff --git")
