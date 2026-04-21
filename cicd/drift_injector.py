"""Mid-episode state/schema drift injection.

Real production systems shift beneath you: configs get rotated, schemas
migrate, API contracts change. To make this env genuinely test world-modeling
(Theme #3.1) and not static puzzle solving, we let the workspace mutate *after*
the agent has already started investigating.

A drift event fires with a low probability during `rerun_pipeline`, but only
*after* the pipeline starts passing — i.e., once the agent thinks they're done,
a new schema-level change invalidates a downstream stage. This forces the
agent to maintain a persistent world model instead of memorizing one fix.

Drift is opt-in and deterministic per episode (keyed by episode_id). Disabled
by default; enable with `META_HACKATHON_DRIFT_ENABLED=true`.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class DriftEvent:
    kind: str
    files_touched: List[str]
    description: str
    hint_keywords: List[str]


def drift_enabled() -> bool:
    return os.getenv("META_HACKATHON_DRIFT_ENABLED", "false").strip().lower() == "true"


def drift_probability() -> float:
    try:
        return max(0.0, min(1.0, float(os.getenv("META_HACKATHON_DRIFT_PROBABILITY", "0.5"))))
    except ValueError:
        return 0.5


# Drift strategies: each one mutates a live workspace file so the *next* pipeline
# run fails in a new way, even though the previously-injected fault was fixed.

def _drift_health_schema(workspace: str) -> Optional[DriftEvent]:
    """Rename the /health endpoint — matching tests now fail."""
    routes = os.path.join(workspace, "services/api/routes.py")
    if not os.path.exists(routes):
        return None
    try:
        with open(routes, "r", encoding="utf-8") as f:
            content = f.read()
        if '"/health"' not in content:
            return None
        new_content = content.replace('"/health"', '"/healthz"')
        with open(routes, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError:
        return None
    return DriftEvent(
        kind="schema_drift_endpoint_rename",
        files_touched=["services/api/routes.py"],
        description="Upstream rotated /health → /healthz; downstream tests and healthcheck now mismatch.",
        hint_keywords=["route", "endpoint", "health", "healthz", "rename"],
    )


def _drift_new_required_dep(workspace: str) -> Optional[DriftEvent]:
    """Inject a bogus hard-pinned dep that breaks resolution."""
    req = os.path.join(workspace, "services/api/requirements.txt")
    if not os.path.exists(req):
        return None
    try:
        with open(req, "r", encoding="utf-8") as f:
            existing = f.read()
        if "itsdangerous==0.0.99" in existing:
            return None
        with open(req, "w", encoding="utf-8") as f:
            f.write(existing.rstrip() + "\nitsdangerous==0.0.99\n")
    except OSError:
        return None
    return DriftEvent(
        kind="dependency_drift_new_pin",
        files_touched=["services/api/requirements.txt"],
        description="Platform team added itsdangerous==0.0.99 policy pin; resolver now fails.",
        hint_keywords=["dependency", "itsdangerous", "pin", "version"],
    )


def _drift_compose_port_shift(workspace: str) -> Optional[DriftEvent]:
    """Change exposed port without updating healthcheck — deploy now fails."""
    compose = os.path.join(workspace, "docker-compose.yml")
    if not os.path.exists(compose):
        return None
    try:
        with open(compose, "r", encoding="utf-8") as f:
            content = f.read()
        if "5000:5000" not in content:
            return None
        new_content = content.replace('"5000:5000"', '"5050:5000"')
        with open(compose, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError:
        return None
    return DriftEvent(
        kind="infra_drift_port_shift",
        files_touched=["docker-compose.yml"],
        description="Infra team shifted external port 5000 → 5050; healthchecks break.",
        hint_keywords=["port", "compose", "healthcheck", "5050"],
    )


def _drift_compose_env_key_move(workspace: str) -> Optional[DriftEvent]:
    """Rename/move compose env keys and inject an invalid port env value.

    This is a visible re-triage drift: after a previously successful rerun,
    deploy fails because compose now depends on an invalid PORT value.
    """
    compose = os.path.join(workspace, "docker-compose.yml")
    if not os.path.exists(compose):
        return None
    try:
        with open(compose, "r", encoding="utf-8") as f:
            content = f.read()

        if "APP_RUNTIME_ENV=production" in content and "${PORT}:5000" in content:
            return None

        new_content = content
        if "FLASK_ENV=production" in new_content:
            new_content = new_content.replace("FLASK_ENV=production", "APP_RUNTIME_ENV=production")

        if '"5000:5000"' in new_content:
            new_content = new_content.replace('"5000:5000"', '"${PORT}:5000"')

        if "PORT=not-a-number" not in new_content and "environment:" in new_content:
            new_content = new_content.replace("environment:\n", "environment:\n      - PORT=not-a-number\n", 1)

        if new_content == content:
            return None

        with open(compose, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError:
        return None

    return DriftEvent(
        kind="infra_drift_compose_env_key_move",
        files_touched=["docker-compose.yml"],
        description=(
            "Runtime compose keys drifted (FLASK_ENV moved to APP_RUNTIME_ENV and PORT mapping externalized); "
            "deploy now fails until env mapping is repaired."
        ),
        hint_keywords=["compose", "environment", "PORT", "invalid", "deploy", "key"],
    )


_DRIFT_STRATEGIES = [
    _drift_compose_env_key_move,
    _drift_health_schema,
    _drift_new_required_dep,
    _drift_compose_port_shift,
]


def maybe_drift(workspace: str, episode_seed: int, attempt_idx: int) -> Optional[DriftEvent]:
    """Possibly mutate workspace state mid-episode. Returns the drift event if fired.

    Called from `rerun_pipeline` after a successful run, so drift only surfaces
    once the agent has achieved a green pipeline — then the world changes.

    `attempt_idx` disambiguates reruns so a single seeded episode can fire
    drift on the first post-success rerun (if lucky) but not the next one.
    """
    if not drift_enabled():
        return None

    rng = random.Random(hash((episode_seed, attempt_idx, "drift")) & 0xFFFFFFFF)
    if rng.random() > drift_probability():
        return None

    order = list(_DRIFT_STRATEGIES)
    rng.shuffle(order)
    for strategy in order:
        event = strategy(workspace)
        if event:
            return event
    return None
