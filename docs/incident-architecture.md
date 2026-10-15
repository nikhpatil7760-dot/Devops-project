# Generic incident investigation and remediation

The two-workflow design keeps `.github/workflows/cicd.yml` as the normal CI/CD pipeline and `.github/workflows/ollama-remediation.yml` as the manual recovery path. The CI/CD workflow sends every executable action through `scripts/incident_pipeline.py`. The recorder is not aware of Kubernetes, Docker, test names, or error strings: it stores the stage and action, exact command, start/end timestamps, exit status, raw stdout/stderr, commit SHA, workflow run URL, and artifact paths. A failed action remains the original job result. The failure investigation step is `continue-on-error` and writes evidence and a report without changing that result.

The investigator selects a small set of repository files from evidence tokens and sends those excerpts, the exact failed stage, and the raw records to Ollama. It can search further by running additional read-only diagnostics, but it must distinguish confirmed evidence from uncertainty. A missing, malformed, or unavailable model response produces an uncertainty report rather than an invented cause. The report is saved as an artifact and appended to `GITHUB_STEP_SUMMARY`.

## Required GitHub configuration

1. The CI workflow uses `contents: read` and `actions: read` to collect run metadata and logs through the GitHub API; artifact upload uses the job token and does not require `actions: write`. The self-hosted runner needs Python, pytest, Docker, and any tools used by the pipeline stages. On the Windows runner, the workflows invoke Python through `py -3.12` because the `python` App Execution Alias may be unavailable; the Python launcher must be installed and available on `PATH`.
2. Create a protected environment named `ollama-remediation-approval` and configure required reviewers. Environment approval is the only approval signal; a workflow input, model response, artifact, or boolean is never treated as approval.
3. The remediation proposal job has `actions: read` and `contents: read`. Only the environment-gated apply job has `contents: write` and `pull-requests: write`, so it can publish a dedicated branch and PR after revalidation. Its `apply_changes` input is only an opt-in to reach that protected job and is never approval. Repository branch protections must require review and prevent direct pushes to `main`.
4. Set `OLLAMA_URL`, `OLLAMA_MODEL`, and optionally `OLLAMA_TIMEOUT_SECONDS` on the runner or as environment configuration. No Ollama secret is required for a local Ollama endpoint; use the runner's secret mechanism for a remote endpoint.

## Lifecycle

Every new failure, including one in a future stage, is recorded and reaches the same investigation step. A remediation proposal is bound by `scripts/proposal_binding.py` to the incident ID, source commit SHA, and SHA-256 of the exact patch. After environment approval, the separate job checks all three identities again, applies only that patch, runs validation, and stops on a changed diff or failed test. A successful validation creates a dedicated branch and pull request; it never pushes directly to `main` or merges.

For infrastructure or transient incidents, the report may recommend read-only diagnostics or a separately approved operational action. It does not create an unrelated application-code patch. After a reviewed PR is merged, the normal CI workflow reruns and any new failure follows this same lifecycle.

## RCA evidence, reports and configuration

On failure, `incident_pipeline.py collect` builds the RCA input from the failed stage's exit code, error lines and log tail; the commit message/author; the diff and changed files since the last successful run of the workflow (GitHub runs + compare API, falling back to local git or the triggering commit); the failed stage's workflow step and related config; stage-specific evidence (pytest/JUnit failures, Docker build errors, registry/image errors); and, for Kubernetes stages, read-only `kubectl` events, `describe pod`, and current and `--previous` pod logs. Shared helpers live in `scripts/rca_context.py`.

Secrets are redacted (token formats, private keys, `Bearer`/`password=`-style values, URL credentials, and values of secret-named environment variables) before anything is written by RCA or sent to the model. Raw `stages/*` records written by the stage wrapper are left unchanged. The prompt is built in priority order (error lines, stage evidence, diff, log tail, Kubernetes, referenced files, config, other files) under a hard character budget; the exact context sent is saved as `rca-context.md`.

The model must return JSON (summary, error lines, root cause with evidence and confidence, other causes, fix steps with diffs, verification, prevention, `insufficient_data`). Error lines not present in the evidence are dropped and unsupported confidence is downgraded. Reports are written to `incident-report-<run_id>.md` (unchanged path, appended to the step summary) and `incident-report-<run_id>.json`. Any collection or model failure produces a deterministic "insufficient data" report; `collect` always exits 0 and never changes the job result.

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_URL` / `OLLAMA_MODEL` | `http://localhost:11434/api/generate` / `llama3.2:3b` | Model endpoint and model |
| `OLLAMA_API_KEY` | unset | Optional bearer token for a remote endpoint (use a runner secret) |
| `OLLAMA_TIMEOUT_SECONDS` / `OLLAMA_NUM_CTX` / `OLLAMA_NUM_PREDICT` | `300` / `6144` / `1200` | Model call limits |
| `RCA_MAX_CONTEXT_CHARS` | `12000` | Hard prompt-context budget |
| `RCA_LOG_TAIL_LINES` / `RCA_MAX_ERROR_LINES` / `RCA_MAX_FILES` | `80` / `60` / `8` | Evidence limits |
| `RCA_COMMAND_TIMEOUT_SECONDS` | `20` | Per git/GitHub/kubectl call |
| `RCA_ENABLE_K8S` | `auto` | `auto`, `always` or `never` |
| `RCA_K8S_NAMESPACE` / `RCA_K8S_SELECTOR` | `flask-app` / `app.kubernetes.io/name=flask-app` | Pods to inspect |
| `RCA_BASE_SHA` | unset | Override the "last successful build" commit |
| `RCA_CONFIG_FILES` / `RCA_REDACT_ENV_NAMES` | unset | Extra config files to include / extra env vars to redact (comma-separated) |

Run the RCA tests with `py -3.12 -m pytest scripts/test_rca_context.py scripts/test_ai_rca.py scripts/test_incident_pipeline_v2.py`.
