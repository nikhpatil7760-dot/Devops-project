from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_workflows_bootstrap_pip_and_use_available_shell():
    ci = (ROOT / ".github" / "workflows" / "cicd.yml").read_text(encoding="utf-8")
    remediation = (ROOT / ".github" / "workflows" / "ollama-remediation.yml").read_text(encoding="utf-8")
    assert "-- py -3.12 -m ensurepip --upgrade" in ci
    assert "shell: powershell" in ci
    assert "shell: powershell" in remediation
    assert "shell: pwsh" not in ci + remediation
