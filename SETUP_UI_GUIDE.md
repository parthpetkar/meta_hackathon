# Setup and UI Testing Guide

This guide explains how to run the Meta Hackathon incident-response environment locally and test it using the OpenEnv web UI.

## 1. Prerequisites

- Docker Desktop installed and running
- Python 3.10+ installed
- `uv` installed
- Project cloned locally

Optional (for baseline model runs):

- `OPENAI_API_KEY`
- `HF_TOKEN`
- `API_BASE_URL`
- `MODEL_NAME`

## 2. Local Setup

From the repository root:

```powershell
uv sync
```

This installs project dependencies inside the local environment used by `uv run`.

## 3. Validate the Environment

Run OpenEnv validation:

```powershell
uv run openenv validate
```

Build Docker image:

```powershell
docker build -t meta_hackathon-env:latest -f Dockerfile .
```

If both pass, your environment is ready for local UI testing.

## 4. Start the Server Locally

Start FastAPI server:

```powershell
uv run python -m server.app --host 0.0.0.0 --port 8000
```

Keep this terminal running.

## 5. Open the UI

In your browser, open:

- OpenEnv UI: `http://localhost:8000/web`
- API docs (optional): `http://localhost:8000/docs`
- Health endpoint: `http://localhost:8000/health`

## 6. UI Testing Flow

### 6.1 Start an episode

1. Open `http://localhost:8000/web`.
2. Click reset/start (depending on UI control labels).
3. Confirm the observation includes:
   - `task_id`
   - `task_title`
   - `difficulty`
   - `recommended_actions`

The environment rotates scenarios in deterministic order when reset repeatedly:

1. `easy`
2. `medium`
3. `hard`

### 6.2 Send structured actions

Use these sample actions in order (one per step):

```json
{"operation":"inspect_alerts","target":"","value":""}
{"operation":"inspect_metrics","target":"","value":""}
{"operation":"inspect_service","target":"<first_service_from_available_services>","value":""}
{"operation":"inspect_logs","target":"<same_service>","value":""}
{"operation":"set_hypothesis","target":"","value":"<task_specific_hypothesis>"}
{"operation":"apply_fix","target":"","value":"<task_specific_fix>"}
{"operation":"verify_fix","target":"","value":""}
```

### 6.3 Task-specific expected fixes

- `easy` -> `scale-cache-cluster`
- `medium` -> `increase-payment-db-pool`
- `hard` -> `rollback-search-rollout`

### 6.4 Expected success signals in UI

- `status` becomes `resolved`
- `incident_resolved` becomes `true`
- Episode `done` becomes `true`
- `final_score` is between `0.0` and `1.0`

## 7. Negative Testing (Important)

These checks verify penalty behavior and episode boundaries.

### 7.1 Repeated noisy action

Repeat `verify_fix` early before applying a fix.

Expected:

- Negative rewards accumulate
- Episode may timeout at max step budget

### 7.2 Destructive fix

Apply one known destructive fix for the active task.

Expected:

- Immediate failure path
- `status` becomes `failed`
- Episode ends (`done=true`)

## 8. Baseline Inference Check

Run baseline script:

```powershell
uv run python inference.py
```

Expected log format:

- `[START] ...`
- `[STEP] ...`
- `[END] ...`

For a healthy run, all three tasks should complete and script should exit with code `0`.

## 9. Quick Troubleshooting

### `openenv` command not found

Run with `uv` instead of global install:

```powershell
uv run openenv validate
```

### Docker build fails

- Ensure Docker Desktop is running
- Retry from repository root
- Rebuild without cache if needed:

```powershell
docker build --no-cache -t meta_hackathon-env:latest -f Dockerfile .
```

### UI does not load

- Confirm server terminal is running
- Check `http://localhost:8000/health`
- Verify port 8000 is not in use by another process

## 10. Pre-Submission Checklist

- `uv run openenv validate` passes
- Docker build passes
- Local UI flow tested for `easy`, `medium`, `hard`
- Positive and negative test cases validated
- `uv run python inference.py` exits successfully with structured logs
