# UI Testing Inputs for Meta Hackathon CI/CD Environment

This file gives copy-paste action inputs for testing your UI quickly.

## Input Schema

Each UI action should send this shape:

```json
{
  "operation": "inspect_pipeline",
  "target": "",
  "value": ""
}
```

Allowed operations:

- inspect_pipeline
- inspect_stage
- inspect_logs
- inspect_git
- inspect_docker
- inspect_tests
- inspect_dependencies
- inspect_permissions
- set_hypothesis
- apply_fix
- verify_fix

## Recommended Env for UI Testing

Set the following in `.env`:

```env
META_HACKATHON_TASK_MODE=easy
```

Valid values are `easy`, `medium`, `hard`, or `cycle`.

## Golden Test Flows

### Easy Task (merge conflict)

1.

```json
{"operation":"inspect_pipeline","target":"","value":""}
```

1.

```json
{"operation":"inspect_stage","target":"merge-check","value":""}
```

1.

```json
{"operation":"inspect_git","target":"","value":""}
```

1.

```json
{"operation":"set_hypothesis","target":"","value":"feature branch is stale and contains unresolved merge conflict"}
```

1.

```json
{"operation":"apply_fix","target":"","value":"resolve-merge-conflict"}
```

1.

```json
{"operation":"verify_fix","target":"","value":""}
```

Expected:

- `done=true` at step 6
- `incident_resolved=true`
- `final_score` near 0.80+

### Medium Task (dependency mismatch)

Set:

```env
META_HACKATHON_TASK_MODE=medium
```

1.

```json
{"operation":"inspect_pipeline","target":"","value":""}
```

1.

```json
{"operation":"inspect_docker","target":"","value":""}
```

1.

```json
{"operation":"inspect_dependencies","target":"","value":""}
```

1.

```json
{"operation":"set_hypothesis","target":"","value":"requests 2.20.0 conflicts with urllib3 constraints required by the app"}
```

1.

```json
{"operation":"apply_fix","target":"","value":"pin-compatible-requests-version"}
```

1.

```json
{"operation":"verify_fix","target":"","value":""}
```

Expected:

- `done=true`
- `incident_resolved=true`
- positive cumulative rewards with strong reward at step 5

### Hard Task (permission + timeout)

Set:

```env
META_HACKATHON_TASK_MODE=hard
```

1.

```json
{"operation":"inspect_pipeline","target":"","value":""}
```

1.

```json
{"operation":"inspect_stage","target":"deploy","value":""}
```

1.

```json
{"operation":"inspect_logs","target":"","value":""}
```

1.

```json
{"operation":"inspect_permissions","target":"","value":""}
```

1.

```json
{"operation":"set_hypothesis","target":"","value":"ci service account lacks registry write permission causing delayed retries and timeout"}
```

1.

```json
{"operation":"apply_fix","target":"","value":"grant-registry-write-permission"}
```

1.

```json
{"operation":"verify_fix","target":"","value":""}
```

Expected:

- `done=true`
- `incident_resolved=true`
- `final_score` in high range when no destructive actions are used

## Negative Tests

### Unsupported operation

```json
{"operation":"inspect_alerts","target":"","value":""}
```

Expected:

- reward around `-0.20`
- metadata contains `error: unsupported operation ...`
- `done=false`

### Destructive fix penalty

```json
{"operation":"apply_fix","target":"","value":"force-push-main"}
```

Expected:

- strong negative reward
- findings include unsafe fix warning
- final score reduced if repeated

## UI Smoke Checklist

- reset returns a task with `task_id`, `difficulty`, `pipeline_status`
- each step updates `action_history`
- `visible_logs` and `visible_metrics` grow after inspect actions
- verify_fix can terminate episode (`done=true`)
- terminal observation includes `final_score` in [0.0, 1.0]
