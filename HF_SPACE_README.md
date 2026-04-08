---
title: Meta Hackathon CI/CD Repair Environment
emoji: "🔨"
colorFrom: red
colorTo: gray
sdk: docker
pinned: false
app_port: 8000
base_path: /web
tags:
  - openenv
  - cicd
  - benchmark
  - rubric-judge
---

## Meta Hackathon CI/CD Repair Environment

This Space hosts a deterministic OpenEnv benchmark for CI/CD incident diagnosis and remediation.

### What the agent does

- Reads logs and config clues
- Forms evidence-backed hypotheses
- Applies safe fixes
- Reruns and verifies fixes
- Finalizes only after resolution is confirmed

### Tasks

- `easy`: merge conflict resolution
- `medium`: dependency + Docker order recovery
- `security`: IAM + secret exposure remediation
- `hard`: multi-service cascade repair with rollback and timeout tuning

### Rubric Judge (Delayed Reward)

This environment supports optional terminal rubric scoring for hypothesis quality.

- Enable with `META_HACKATHON_RUBRIC_ENABLED=true`
- Blend control with `META_HACKATHON_RUBRIC_WEIGHT`
- Timeout control with `META_HACKATHON_RUBRIC_TIMEOUT_SECONDS`
- Model override with `META_HACKATHON_RUBRIC_MODEL`
- Debug logs with `META_HACKATHON_RUBRIC_DEBUG=true`

Scoring blend at terminal step:

`final = (1 - w) * deterministic + w * rubric`

where `w = META_HACKATHON_RUBRIC_WEIGHT`.

### API

- `POST /reset`
- `POST /step`
- `GET /state`

### Local run

```bash
uv sync
uv run uvicorn server.app:app --host 0.0.0.0 --port 8000
```

### Evaluation

```bash
uv run evaluate
```

For design rationale and extension guidance, see `DESIGN.md` in this repository.
