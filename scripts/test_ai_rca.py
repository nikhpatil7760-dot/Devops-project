import importlib.util
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.error import URLError


MODULE_PATH = Path(__file__).with_name("ai-rca.py")
SPEC = importlib.util.spec_from_file_location("ai_rca", MODULE_PATH)
ai_rca = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ai_rca)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


VALID_ANALYSIS = """Incident: The image-loading stage failed.
Evidence: The pipeline evidence shows the requested image was not found.
Root Cause: The image repository name was incorrect.
Recommended Remediation: Build and load the correctly named image.
Rollback Recommendation: No rollback was required before Kubernetes deployment."""


class ReportOutputTests(TestCase):
    def run_main(self, evidence, report_name, *extra_args):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_path = root / "evidence.txt"
            report_path = root / report_name
            evidence_path.write_text(evidence, encoding="utf-8")
            argv = [
                "ai-rca.py",
                "--evidence-file",
                str(evidence_path),
                "--report-output",
                str(report_path),
                *extra_args,
            ]
            with patch.object(sys, "argv", argv):
                result = ai_rca.main()
            return result, report_path.read_text(encoding="utf-8")

    def test_valid_ollama_report_is_saved(self):
        with patch.object(
            ai_rca.urllib.request,
            "urlopen",
            return_value=FakeResponse({"response": VALID_ANALYSIS}),
        ):
            result, report = self.run_main("STATUS DETERMINATION: DEGRADED", "nested/report.md")
        self.assertEqual(result, 0)
        self.assertIn("=== Ollama RCA", report)
        self.assertIn("Root Cause:", report)

    def test_ollama_failure_saves_deterministic_fallback(self):
        evidence = (
            "PIPELINE FAILURE:\n"
            "Failed stage: Load intentionally invalid image name.\n"
            "Kubernetes deployment was not attempted, so the existing application was not changed."
        )
        with patch.object(
            ai_rca.urllib.request,
            "urlopen",
            side_effect=URLError("offline"),
        ):
            result, report = self.run_main(evidence, "fallback.md")
        self.assertEqual(result, 2)
        self.assertIn("Kubernetes deployment was skipped", report)
        self.assertIn("No rollback was required", report)
        self.assertIn("Next Step:", report)

    def test_stage_marker_reports_actual_failed_stage(self):
        evidence = (
            "PIPELINE STAGE MARKER:\n"
            "Select existing Kind cluster\n"
            "PIPELINE STAGE OUTPUT:\n"
            "error: no context exists with the name: kind-flask-plat\n"
        )
        with patch.object(
            ai_rca.urllib.request,
            "urlopen",
            side_effect=URLError("offline"),
        ):
            result, report = self.run_main(evidence, "missing-context.md")
        self.assertEqual(result, 2)
        self.assertIn("failed during Select existing Kind cluster", report)
        self.assertIn("context that does not exist", report)
        self.assertIn("Kubernetes deployment was not attempted", report)

    def test_evidence_only_does_not_call_ollama(self):
        with TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "evidence.txt"
            evidence_path.write_text("captured evidence", encoding="utf-8")
            with patch.object(
                ai_rca.urllib.request,
                "urlopen",
                side_effect=AssertionError("Ollama must not be called"),
            ):
                with patch.object(
                    sys,
                    "argv",
                    ["ai-rca.py", "--evidence-file", str(evidence_path), "--evidence-only"],
                ):
                    self.assertEqual(ai_rca.main(), 0)

    def test_invalid_ollama_output_saves_fallback(self):
        with patch.object(
            ai_rca.urllib.request,
            "urlopen",
            return_value=FakeResponse({"response": "not a labeled report"}),
        ):
            result, report = self.run_main("STATUS DETERMINATION: DEGRADED", "invalid.md")
        self.assertEqual(result, 1)
        self.assertIn("Incident:", report)
        self.assertIn("Recovery:", report)


if __name__ == "__main__":
    import unittest

    unittest.main()

