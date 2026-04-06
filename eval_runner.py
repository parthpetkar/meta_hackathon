"""Deterministic benchmark evaluator for the Meta Hackathon environment."""

from __future__ import annotations

import os
from collections import Counter
from typing import Dict, List, Tuple

try:
    from .server.meta_hackathon_environment import MetaHackathonEnvironment
    from .models import MetaHackathonAction
except ImportError:
    from server.meta_hackathon_environment import MetaHackathonEnvironment
    from models import MetaHackathonAction

SUCCESS_SCORE_THRESHOLD = float(os.getenv("SUCCESS_SCORE_THRESHOLD", "0.20"))
EPISODES_PER_TASK = int(os.getenv("EVAL_EPISODES_PER_TASK", "3"))
TASK_ORDER = ["easy", "medium", "hard"]


def scripted_action(task_name: str, step: int) -> Tuple[str, str, str]:
    plans: Dict[str, List[Tuple[str, str, str]]] = {
        "easy": [
            ("view_logs", "build", ""),
            ("inspect_config", "build", ""),
            ("set_hypothesis", "", "merge conflict from stale feature branch"),
            ("modify_config", "build", "sync branch and resolve merge conflict"),
            ("rerun_pipeline", "", ""),
            ("set_hypothesis", "", "contract tests failing due to stale branch baseline"),
            ("modify_config", "test", "rebase feature branch to refresh contract baseline"),
            ("rerun_pipeline", "", ""),
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
            ("finalize", "", ""),
        ],
        "hard": [
            ("view_logs", "deploy", ""),
            ("inspect_config", "deploy", ""),
            ("inspect_permissions", "", ""),
            ("set_hypothesis", "", "registry write permission missing for ci-runner"),
            ("modify_config", "deploy", "grant artifactregistry writer to ci-runner"),
            ("rerun_pipeline", "", ""),
            ("set_hypothesis", "", "rollout timeout requires tuning after auth recovery"),
            ("modify_config", "deploy", "increase rollout timeout to 20m"),
            ("rerun_pipeline", "", ""),
            ("finalize", "", ""),
        ],
    }
    sequence = plans.get(task_name, plans["medium"])
    if step <= len(sequence):
        return sequence[step - 1]
    return sequence[-1]


def run_episode(task_name: str, env: MetaHackathonEnvironment) -> dict:
    observation = env.reset()
    total_reward = 0.0
    steps = 0

    max_steps = int((observation.metadata or {}).get("max_steps", 12))
    variant_id = str((observation.metadata or {}).get("variant_id", "unknown"))

    for step in range(1, max_steps + 1):
        if observation.done:
            break

        operation, target, value = scripted_action(task_name, step)
        result = env.step(MetaHackathonAction(operation=operation, target=target, value=value))
        observation = result
        steps = step
        total_reward += float(result.reward or 0.0)

        if observation.done:
            break

    score = float(observation.final_score)
    resolved = bool(observation.incident_resolved)
    success = resolved and score >= SUCCESS_SCORE_THRESHOLD

    failure_reason = ""
    if not resolved:
        findings = list(observation.findings)
        failure_reason = findings[-1] if findings else "unresolved"

    return {
        "task": task_name,
        "variant_id": variant_id,
        "steps": steps,
        "reward_sum": round(total_reward, 3),
        "score": score,
        "resolved": resolved,
        "success": success,
        "failure_reason": failure_reason,
    }


def main() -> None:
    all_results: List[dict] = []

    print(f"[EVAL] episodes_per_task={EPISODES_PER_TASK} success_threshold={SUCCESS_SCORE_THRESHOLD:.2f}")

    for task_name in TASK_ORDER:
        env = MetaHackathonEnvironment(task_key=task_name)
        for episode in range(EPISODES_PER_TASK):
            result = run_episode(task_name, env)
            all_results.append(result)
            print(
                "[EP] "
                f"task={task_name} episode={episode + 1} variant={result['variant_id']} "
                f"steps={result['steps']} resolved={str(result['resolved']).lower()} "
                f"score={result['score']:.3f} success={str(result['success']).lower()}"
            )

    print("\n[SUMMARY]")
    for task_name in TASK_ORDER:
        task_results = [item for item in all_results if item["task"] == task_name]
        count = len(task_results)
        resolved_rate = sum(1 for item in task_results if item["resolved"]) / max(count, 1)
        success_rate = sum(1 for item in task_results if item["success"]) / max(count, 1)
        avg_score = sum(item["score"] for item in task_results) / max(count, 1)
        avg_steps = sum(item["steps"] for item in task_results) / max(count, 1)
        print(
            f"- task={task_name} resolved_rate={resolved_rate:.2%} "
            f"success_rate={success_rate:.2%} avg_score={avg_score:.3f} avg_steps={avg_steps:.2f}"
        )

    unresolved = [item for item in all_results if not item["resolved"]]
    if unresolved:
        reasons = Counter(item["failure_reason"] for item in unresolved)
        print("\n[TOP_FAILURE_REASONS]")
        for reason, count in reasons.most_common(3):
            print(f"- count={count} reason={reason}")


if __name__ == "__main__":
    main()
