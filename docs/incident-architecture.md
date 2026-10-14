# Generic incident investigation and remediation

The CI/CD workflow sends every executable action through `scripts/incident_pipeline.py`. The recorder is not aware of Kubernetes, Docker, test names, or error strings: it stores the stage and action, exact command, start/end timestamps, exit status, raw stdout/stderr, commit SHA, workflow run URL, and artifact paths. A failed action remains the original job result. The failure investigation step is `continue-on-error` and writes evidence and a report without changing that result.

The investigator selects a small set of repository files from evidence tokens and sends those excerpts, the exact failed stage, and the raw records to Ollama. It can search further by running additional read-only diagnostics, but it must distinguish confirmed evidence from uncertainty. A missing, malformed, or unavailable model response produces an uncertainty report rather than an invented cause. The report is saved as an artifact and appended to `GITHUB_STEP_SUMMARY`.

## Required GitHub configuration

1. The CI workflow needs `contents: read` and `actions: write` only if a later implementation uploads artifacts with a separate token; the current upload action uses the job token. The self-hosted runner needs Python, pytest, Docker, and any tools used by the pipeline stages. On the Windows runner, the workflows invoke Python through `py -3.12` because the `python` App Execution Alias may be unavailable; the Python launcher must be installed and available on `PATH`.
2. Create a protected environment named `ollama-remediation-approval` and configure required reviewers. Environment approval is the only approval signal; a workflow input, model response, artifact, or boolean is never treated as approval.
3. The remediation workflow has `actions: read` and `contents: write` so it can download the exact incident artifact and publish a branch/PR. Repository branch protections must require review and prevent direct pushes to `main`.
4. Set `OLLAMA_URL`, `OLLAMA_MODEL`, and optionally `OLLAMA_TIMEOUT_SECONDS` on the runner or as environment configuration. No Ollama secret is required for a local Ollama endpoint; use the runner's secret mechanism for a remote endpoint.

## Lifecycle

Every new failure, including one in a future stage, is recorded and reaches the same investigation step. A remediation proposal is bound by `scripts/proposal_binding.py` to the incident ID, source commit SHA, and SHA-256 of the exact patch. After environment approval, the separate job checks all three identities again, applies only that patch, runs validation, and stops on a changed diff or failed test. A successful validation creates a dedicated branch and pull request; it never pushes directly to `main` or merges.

For infrastructure or transient incidents, the report may recommend read-only diagnostics or a separately approved operational action. It does not create an unrelated application-code patch. After a reviewed PR is merged, the normal CI workflow reruns and any new failure follows this same lifecycle.
