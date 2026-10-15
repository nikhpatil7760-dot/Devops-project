from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pipeline_uses_real_kind_context_and_no_gh_cli_dependency():
    workflow = (ROOT / ".github" / "workflows" / "cicd.yml").read_text(encoding="utf-8")
    assert "kind-flask-platform" in workflow
    production_line = next(line for line in workflow.splitlines() if "Select existing Kind cluster" in line and "--stage" in line)
    assert "kind-flask-platform" in production_line
    assert "kind-flask-plat" in workflow
    assert "Invoke-RestMethod" in workflow
    assert "gh run view" not in workflow
    assert "shell: powershell" in workflow
    assert "incident.json" in workflow
    assert "incident recorder unavailable" in workflow
    assert "Original pipeline result remains authoritative" in workflow
