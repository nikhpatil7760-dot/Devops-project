import importlib.util
import io
import json
import zipfile
from pathlib import Path


def load(name, filename=None):
    path = Path(__file__).with_name(filename or f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rca = load("rca_context")
incident_pipeline = load("incident_pipeline")
ai_rca = load("ai_rca", "ai-rca.py")


class Response:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


# ---------------------------------------------------------------- redaction

def test_redacts_known_secret_formats():
    text = "\n".join([
        "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "aws AKIAABCDEFGHIJKLMNOP",
        "Authorization: Bearer abc.def-ghi_jkl12345",
        "DATABASE_PASSWORD=hunter2hunter2",
        '{"api_key": "sk-ant-abcdefghijklmnopqrstuvwx"}',
        "clone https://user:s3cretpass@example.com/repo.git",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
        "slack xoxb-1234567890-abcdefghij",
    ])
    redacted, count = rca.redact_count(text)
    for secret in ("ghp_abcdef", "AKIAABCDEFGHIJKLMNOP", "abc.def-ghi_jkl12345", "hunter2hunter2",
                   "sk-ant-abcdef", "s3cretpass", "MIIEow", "xoxb-1234567890"):
        assert secret not in redacted
    assert count >= 8
    assert "https://user:[REDACTED]@example.com" in redacted


def test_redacts_secret_environment_values(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "value-from-env-123")
    monkeypatch.setenv("CUSTOM_VALUE", "custom-sensitive-456")
    monkeypatch.setenv("RCA_REDACT_ENV_NAMES", "CUSTOM_VALUE")
    redacted = rca.redact("echo value-from-env-123 custom-sensitive-456")
    assert "value-from-env-123" not in redacted and "custom-sensitive-456" not in redacted


def test_redaction_keeps_code_and_ordinary_text_readable():
    text = '\n'.join([
        'token = os.getenv("GH_TOKEN")',
        "GH_TOKEN: ${{ github.token }}",
        "author: Nikhil",
        "error: no context exists with the name: kind-flask-plat",
    ])
    assert rca.redact(text) == text


def test_redact_data_handles_nested_structures():
    data = {"a": ["password=topsecret1"], "b": {"c": "ghp_abcdefghijklmnopqrstuvwxyz0123456789"}, "n": 3}
    redacted = rca.redact_data(data)
    assert "topsecret1" not in json.dumps(redacted) and "ghp_" not in json.dumps(redacted)
    assert redacted["n"] == 3


# ---------------------------------------------------------------- truncation

def test_compact_text_respects_limit_and_keeps_tail():
    text = "start\n" + "x" * 10000 + "\nFINAL ERROR LINE"
    compacted = rca.compact_text(text, 500)
    assert len(compacted) <= 500
    assert compacted.endswith("FINAL ERROR LINE")
    assert "chars omitted" in compacted


def test_prompt_context_is_capped_and_prioritizes_errors_and_diff():
    sections = [
        ("Errors", "ERROR: the real failure", 0.3),
        ("Diff", "diff --git a/x b/x\n+changed line", 0.3),
        ("Repository", "filler " * 20000, None),
    ]
    text, truncated = rca.build_prompt_context(sections, 2000)
    assert len(text) <= 2000
    assert truncated
    assert "ERROR: the real failure" in text and "+changed line" in text
    assert text.index("Errors") < text.index("Diff") < text.index("Repository")


def test_error_lines_are_exact_deduplicated_and_limited():
    log = "ok\nERROR: boom\nall good\n0 failed\nERROR: boom\nTraceback (most recent call last):\n"
    lines = rca.error_lines(log, limit=10)
    assert lines == ["L5: ERROR: boom", "L6: Traceback (most recent call last):"]
    assert len(rca.error_lines("\n".join(f"error {i}" for i in range(100)), limit=5)) == 5


def test_error_lines_catch_python_and_shell_failures():
    log = "\n".join([
        r"C:\Python\python.exe: No module named pytest",
        "ModuleNotFoundError: No module named 'flask'",
        "'kind' is not recognized as an internal or external command",
        "bash: kubectl: command not found",
    ])
    assert len(rca.error_lines(log)) == 4


# ---------------------------------------------------------------- stage-specific evidence

def test_stage_evidence_extracts_pytest_docker_and_registry_errors():
    log = "\n".join([
        "FAILED app/test_app.py::test_health - assert 500 == 200",
        "E       assert 500 == 200",
        "app/test_app.py:25: AssertionError",
        "ERROR: failed to solve: process \"/bin/sh -c pip install -r requirements.txt\" did not complete successfully: exit code: 1",
        "Dockerfile:12",
        "Error response from daemon: pull access denied for devops-platform, repository does not exist",
    ])
    evidence = rca.stage_evidence(log)
    assert any("test_health" in line for line in evidence["pytest"])
    assert any("failed to solve" in line for line in evidence["docker"])
    assert any("pull access denied" in line for line in evidence["registry"])


def test_junit_failures_are_parsed(tmp_path):
    (tmp_path / "junit.xml").write_text(
        '<testsuite><testcase classname="app.test_app" name="test_ok"/>'
        '<testcase classname="app.test_app" name="test_bad"><failure message="assert 1 == 2">trace</failure></testcase></testsuite>'
    )
    failures = rca.junit_failures(tmp_path)
    assert failures == ["app.test_app::test_bad [failure] assert 1 == 2"]


def test_referenced_files_resolve_absolute_and_relative_paths(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "app.py").write_text("a\nb\nc\n")
    log = f'File "{tmp_path / "app" / "app.py"}", line 2, in index\napp/app.py:3: AssertionError\n/usr/lib/site-packages/x.py:1'
    refs = rca.referenced_files(log, root=tmp_path)
    assert refs == [("app/app.py", 3), ("app/app.py", 2)]
    assert ">" in rca.file_snippet("app/app.py", 2, root=tmp_path).splitlines()[2]


def test_kubernetes_collection_is_selected_for_kubernetes_stages():
    config = rca.settings()
    assert rca.wants_kubernetes("Apply application", "kubectl apply -f kubernetes/deployment.yaml", "", config)
    assert not rca.wants_kubernetes("Run tests", "py -m pytest", "assert 1 == 2", config)
    assert rca.wants_kubernetes("Validate", "py check.py", "Back-off: CrashLoopBackOff", config)


# ---------------------------------------------------------------- AI output parsing

CONTEXT = "### Errors\nL3: error: no context exists with the name: kind-flask-plat\n--- .github/workflows/cicd.yml:43 ---"


def test_parse_ai_report_accepts_fenced_json_and_grounds_lines():
    answer = "```json\n" + json.dumps({
        "summary": "The kubectl context does not exist.",
        "failed_stage": "Select existing Kind cluster",
        "error_lines": ["error: no context exists with the name: kind-flask-plat", "invented line that is not in evidence"],
        "root_cause": {"description": "Wrong context name.", "confidence": "high",
                       "evidence": [{"source": ".github/workflows/cicd.yml:43", "detail": "no context exists with the name: kind-flask-plat"}]},
        "other_possible_causes": ["Cluster was deleted"],
        "fix_steps": [{"description": "Use kind-flask-platform", "diff": "-a\n+b"}],
        "verification": ["kubectl config use-context kind-flask-platform"],
        "prevention": ["Validate context names in CI"],
        "insufficient_data": False,
    }) + "\n```"
    report = rca.parse_ai_report(answer, CONTEXT)
    assert report["error_lines"] == ["error: no context exists with the name: kind-flask-plat"]
    assert report["root_cause"]["confidence"] == "high"
    assert report["root_cause"]["evidence"][0]["verified"] is True
    assert report["other_possible_causes"] == [{"description": "Cluster was deleted", "confidence": "low"}]


def test_parse_ai_report_downgrades_unsupported_confidence():
    answer = json.dumps({"summary": "s", "root_cause": {"description": "guess", "confidence": "high",
                                                         "evidence": [{"source": "nowhere.py:1", "detail": "made up"}]}})
    report = rca.parse_ai_report(answer, CONTEXT)
    assert report["root_cause"]["confidence"] == "low"
    assert report["root_cause"]["evidence"][0]["verified"] is False


def test_fix_diff_that_repeats_the_breaking_change_is_flagged():
    context = "### Git diff\n-  <h1>Welcome to the world</h1>\n+  <h1>Welcome to our world</h1>\n"
    echoed = "--- a/x\n+++ b/x\n-  <h1>Welcome to the world</h1>\n+  <h1>Welcome to our world</h1>"
    reverted = "--- a/x\n+++ b/x\n-  <h1>Welcome to our world</h1>\n+  <h1>Welcome to the world</h1>"
    answer = {"summary": "s", "root_cause": {"description": "d"},
              "fix_steps": [{"description": "echo", "diff": echoed}, {"description": "revert", "diff": reverted}]}
    steps = rca.parse_ai_report(json.dumps(answer), context)["fix_steps"]
    assert "warning" in steps[0] and "warning" not in steps[1]


def test_parse_ai_report_marks_insufficient_data():
    answer = json.dumps({"summary": "s", "root_cause": {"description": "Insufficient data", "confidence": "insufficient_data"},
                         "missing_data": ["pod logs"]})
    report = rca.parse_ai_report(answer, CONTEXT)
    assert report["insufficient_data"] is True and report["missing_data"] == ["pod logs"]


def test_parse_ai_report_rejects_unusable_output():
    for answer in ("no json here", "[1, 2]", json.dumps({"summary": "", "root_cause": {}})):
        try:
            rca.parse_ai_report(answer, CONTEXT)
        except ValueError:
            continue
        raise AssertionError(f"accepted {answer!r}")


# ---------------------------------------------------------------- pipeline integration

def test_github_job_logs_zip_is_read_without_crashing(tmp_path, monkeypatch):
    """Regression: io was not imported, so collection crashed whenever GH_TOKEN was set."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.setenv("GH_TOKEN", "test-token-value")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("1_build.txt", "##[error]Process completed with exit code 1.")
    responses = iter([Response({"jobs": [{"id": 5, "name": "pipeline", "steps": [{"name": "Run tests", "conclusion": "failure", "number": 7}]}]}),
                      Response(archive.getvalue())])
    monkeypatch.setattr(incident_pipeline.urllib.request, "urlopen", lambda *a, **k: next(responses))
    result = incident_pipeline.collect_github_actions_logs(tmp_path)
    assert result["logs_available"] is True
    assert result["failed_steps"][0]["step"] == "Run tests"
    assert "exit code 1" in (tmp_path / "github-actions.log").read_text()


def _failing_incident(tmp_path, monkeypatch, output):
    directory = tmp_path / "incidents"
    monkeypatch.setattr(incident_pipeline, "INCIDENT_DIR", directory)
    monkeypatch.setenv("RCA_ENABLE_K8S", "never")
    monkeypatch.setenv("SECRET_DEPLOY_TOKEN", "leaky-secret-value-99")
    command = ["py", "-3.12", "-c", f"import sys; print({output!r}); sys.exit(4)"]
    incident_pipeline.run_stage("Run tests", command)
    return directory, *incident_pipeline.build_incident(directory)


def test_ai_report_is_written_as_markdown_and_json_with_redacted_prompt(tmp_path, monkeypatch):
    directory, incident_path, incident = _failing_incident(
        tmp_path, monkeypatch, "FAILED app/test_app.py::test_health - leaky-secret-value-99\nE       assert 500 == 200")
    assert "leaky-secret-value-99" not in incident_path.read_text()
    assert incident["error_lines"] and incident["stage_evidence"]["pytest"]
    sent = {}

    def fake_urlopen(request, timeout):
        sent["body"] = json.loads(request.data)
        return Response({"response": json.dumps({
            "summary": "A test assertion failed.",
            "error_lines": ["E       assert 500 == 200"],
            "root_cause": {"description": "test_health got 500.", "confidence": "medium",
                           "evidence": [{"source": "log of Run tests", "detail": "assert 500 == 200"}]},
            "fix_steps": [{"description": "Fix the handler", "diff": ""}],
            "verification": ["py -3.12 -m pytest app/test_app.py"],
            "prevention": ["Add a unit test for the handler"],
            "insufficient_data": False,
        })})

    monkeypatch.setattr(incident_pipeline.urllib.request, "urlopen", fake_urlopen)
    report_path = tmp_path / "incident-report-1.md"
    assert incident_pipeline.investigate(incident_path, report_path) == 0
    assert "leaky-secret-value-99" not in sent["body"]["prompt"]
    assert sent["body"]["format"] == "json"
    assert len(sent["body"]["prompt"]) < rca.settings()["max_context_chars"] + 4000
    markdown = report_path.read_text()
    report = json.loads(report_path.with_suffix(".json").read_text())
    assert "## Root cause or uncertainty" in markdown and "**Confidence:** medium" in markdown
    assert report["analysis"]["source"] == "ollama"
    assert report["failed_stage"] == "Run tests" and report["exit_code"] == 4
    assert report["error_lines"] == ["E       assert 500 == 200"]
    for key in ("summary", "root_cause", "other_possible_causes", "fix_steps", "verification", "prevention", "insufficient_data"):
        assert key in report


def test_fallback_report_when_ai_is_unavailable(tmp_path, monkeypatch):
    directory, incident_path, _ = _failing_incident(tmp_path, monkeypatch, "ERROR: something broke")
    monkeypatch.setattr(incident_pipeline.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    report_path = tmp_path / "report.md"
    assert incident_pipeline.investigate(incident_path, report_path) == 0
    report = json.loads(report_path.with_suffix(".json").read_text())
    assert report["analysis"]["source"] == "fallback" and "offline" in report["analysis"]["fallback_reason"]
    assert report["insufficient_data"] is True
    assert any("ERROR: something broke" in line for line in report["error_lines"])
    assert "insufficient data" in report_path.read_text().lower()


def test_collect_never_fails_even_when_collection_crashes(tmp_path, monkeypatch):
    monkeypatch.setattr(incident_pipeline, "INCIDENT_DIR", tmp_path / "incidents")
    monkeypatch.setattr(incident_pipeline, "build_incident", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(incident_pipeline.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    report_path = tmp_path / "report.md"
    assert incident_pipeline.collect(report_path) == 0
    assert report_path.exists() and report_path.with_suffix(".json").exists()


def test_ai_rca_redacts_evidence_before_sending(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("STATUS DETERMINATION: DEGRADED\npassword=supersecret123\n", encoding="utf-8")
    sent = {}

    def fake_urlopen(request, timeout):
        sent["prompt"] = json.loads(request.data)["prompt"]
        raise OSError("offline")

    monkeypatch.setattr(ai_rca.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("sys.argv", ["ai-rca.py", "--evidence-file", str(evidence), "--report-output", str(tmp_path / "r.md")])
    assert ai_rca.main() == 2
    assert "supersecret123" not in sent["prompt"]
