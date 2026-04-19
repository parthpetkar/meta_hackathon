"""Deterministic fallback action plans for each benchmark task."""

from typing import Dict, List, Tuple


def fallback_action(task_name: str, step: int) -> Tuple[str, str, str]:
    plans: Dict[str, List[Tuple[str, str, str]]] = {
        "easy": [
            ("view_logs", "build", ""),
            ("inspect_config", "build", ""),
            ("set_hypothesis", "", "merge conflict markers are blocking build validation"),
            ("modify_config", "build", "sync branch and resolve merge conflict"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
        "flaky": [
            ("view_logs", "test", ""),
            ("inspect_config", "test", ""),
            ("set_hypothesis", "", "flaky timing-sensitive test is intermittently failing in CI"),
            ("modify_config", "test", "add retry policy for flaky test isolation"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
        "medium": [
            ("view_logs", "build", ""),
            ("inspect_config", "build", ""),
            ("inspect_dockerfile", "build", ""),
            ("set_hypothesis", "", "requests and urllib3 are incompatible"),
            ("add_dependency", "build", "pin compatible requests urllib3 versions"),
            ("rerun_pipeline", "", ""),
            ("set_hypothesis", "", "docker install order mismatch still causing flaky build"),
            ("modify_config", "build", "reorder docker install steps"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
        "network": [
            ("view_logs", "deploy", ""),
            ("inspect_config", "deploy", ""),
            ("inspect_permissions", "deploy", ""),
            ("set_hypothesis", "", "transient network dns outage is blocking artifact upload"),
            ("modify_config", "deploy", "configure retry backoff for artifact upload with dns fallback"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
        "security": [
            ("view_logs", "deploy", ""),
            ("inspect_permissions", "deploy", ""),
            ("set_hypothesis", "", "artifact registry push fails because deployer lacks writer permissions"),
            ("modify_config", "deploy", "grant artifactregistry writer to ci-deployer"),
            ("rerun_pipeline", "", ""),
            ("view_logs", "deploy", ""),
            ("inspect_dockerfile", "build", ""),
            ("set_hypothesis", "", "Dockerfile exposes API_KEY and must use secret manager reference"),
            ("modify_config", "deploy", "replace Dockerfile API_KEY with secret manager reference"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
        "hard": [
            ("inspect_permissions", "build", ""),
            ("set_hypothesis", "", "service-a publish is failing because artifactregistry writer permission is missing"),
            ("modify_config", "build", "grant artifactregistry writer to service-a publisher"),
            ("rerun_pipeline", "", ""),
            ("inspect_config", "deploy", ""),
            ("set_hypothesis", "", "service-b should rollback to the last stable image revision"),
            ("modify_config", "deploy", "rollback service-b to stable image revision"),
            ("rerun_pipeline", "", ""),
            ("view_logs", "deploy", ""),
            ("set_hypothesis", "", "service-b rollout timeout should be increased to 20m after rollback"),
            ("modify_config", "deploy", "increase rollout timeout to 20m"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
    }
    sequence = plans.get(task_name, plans["medium"])
    if step <= len(sequence):
        return sequence[step - 1]

    # After the base sequence, retry canonical diagnose/fix/verify loop.
    tail: Dict[str, List[Tuple[str, str, str]]] = {
        "easy": [
            ("modify_config", "build", "sync branch and resolve merge conflict"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
        "flaky": [
            ("set_hypothesis", "", "flaky test still needs retry-safe isolation"),
            ("modify_config", "test", "add retry wrapper for flaky test"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
        "medium": [
            ("set_hypothesis", "", "dependency or docker order mismatch still active"),
            ("modify_config", "build", "reorder docker install steps"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
        "network": [
            ("set_hypothesis", "", "artifact upload outage still needs transient network handling"),
            ("modify_config", "deploy", "configure retry backoff and proxy fallback for artifact upload"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
        "security": [
            ("set_hypothesis", "", "iam role or secret manager mapping still incomplete"),
            ("modify_config", "deploy", "grant writer and use secret manager API_KEY"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
        "hard": [
            ("set_hypothesis", "", "service-b timeout likely still below 20m after rollback and needs tuning"),
            ("modify_config", "deploy", "grant service-a writer then rollback service-b and set timeout 20m"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
    }
    tail_sequence = tail.get(task_name, tail["medium"])
    index = (step - len(sequence) - 1) % len(tail_sequence)
    return tail_sequence[index]

