"""Orchestration loop for the agentic inference baseline."""

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import requests

from .actions import (
    action_matches_expected_plan,
    normalize_model_action,
    pre_finalize_guard_action,
    progression_guard_action,
    ready_to_finalize,
    should_force_fallback,
)
from .config import (
    API_BASE_URL,
    API_KEY,
    BENCHMARK,
    INFERENCE_VERBOSE,
    MAX_CONSECUTIVE_TOOL_CALL_MISSES,
    MAX_MODEL_CALLS_PER_TASK,
    MAX_STEPS,
    MIN_MODEL_CALLS_BEFORE_FORCED_FALLBACK,
    MODEL_NAME,
    PREFER_DETERMINISTIC_ACTIONS,
    RESCUE_ON_NEGATIVE_REWARD,
    SUCCESS_SCORE_THRESHOLD,
    TASK_ORDER,
)
from .fallback import fallback_action
from .http_environment import format_obs_for_llm, reset_env, step_env, trim_messages
from .model_client import get_model_action
from .prompts import build_system_prompt
from .trajectory_logging import log_detail, log_end, log_start, log_step

try:
    from ..models import MetaHackathonObservation
except ImportError:  # pragma: no cover - direct script execution
    from models import MetaHackathonObservation

if TYPE_CHECKING:
    from openai import OpenAI


def run_task(client: "OpenAI", session: requests.Session, fallback_task_name: str) -> Tuple[str, bool, int, float]:
    history: List[str] = []
    rewards: List[float] = []
    steps_taken = 0
    score = 0.0
    success = False
    resolved = False
    task_name = fallback_task_name
    fallback_window = 0
    tool_call_misses = 0
    model_calls_used = 0
    disable_model_calls = MAX_MODEL_CALLS_PER_TASK <= 0
    observation: Optional[MetaHackathonObservation] = None
    messages: List[Dict[str, Any]] = []
    task_max_steps = MAX_STEPS

    try:
        observation = reset_env(session)
        observed = observation.metadata or {}
        if isinstance(observed, dict) and observed.get("task_key"):
            task_name = str(observed.get("task_key"))
        if isinstance(observed, dict) and observed.get("max_steps"):
            task_max_steps = max(task_max_steps, int(observed.get("max_steps", MAX_STEPS)))

        messages = [{"role": "system", "content": build_system_prompt(task_name)}]

        log_start(task=task_name, env=BENCHMARK, model=MODEL_NAME)
        if INFERENCE_VERBOSE:
            log_detail(
                step=0,
                action="reset",
                observation=observation,
                reward=float(observation.reward or 0.0),
                done=bool(observation.done),
                error=None,
            )

        task_title = observation.task_title or task_name
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Task: {task_title}\n\n{format_obs_for_llm(observation, 0)}\n\n"
                    "Begin debugging."
                ),
            }
        )

        for step in range(1, task_max_steps + 1):
            if observation.done:
                break

            if disable_model_calls:
                operation, target, value = fallback_action(task_name, step)
                assistant_message = {
                    "role": "assistant",
                    "content": "Model tool-calling disabled after repeated misses; using deterministic fallback.",
                }
                tool_call_id = None
            else:
                operation, target, value, assistant_message, tool_call_id = get_model_action(
                    client=client,
                    step=step,
                    messages=messages,
                )
                model_calls_used += 1
                if model_calls_used >= MAX_MODEL_CALLS_PER_TASK:
                    disable_model_calls = True

                if tool_call_id is None:
                    tool_call_misses += 1
                    if (
                        tool_call_misses >= MAX_CONSECUTIVE_TOOL_CALL_MISSES
                        and model_calls_used >= MIN_MODEL_CALLS_BEFORE_FORCED_FALLBACK
                    ):
                        disable_model_calls = True
                else:
                    tool_call_misses = 0

            operation, target, value = normalize_model_action(
                operation=operation,
                target=target,
                value=value,
                step=step,
            )

            use_fallback = False
            if fallback_window > 0:
                use_fallback = True
                fallback_window -= 1
            elif RESCUE_ON_NEGATIVE_REWARD and rewards and rewards[-1] < 0.0:
                use_fallback = True
                fallback_window = 3
            elif not operation:
                use_fallback = True
                fallback_window = 2
            elif should_force_fallback(
                step=step,
                rewards=rewards,
                history=history,
                observation=observation,
            ):
                use_fallback = True
                fallback_window = 2
            elif PREFER_DETERMINISTIC_ACTIONS and not action_matches_expected_plan(
                task_name,
                step,
                operation,
                target,
                value,
            ):
                use_fallback = True

            if use_fallback:
                operation, target, value = fallback_action(task_name, step)
                assistant_message = {
                    "role": "assistant",
                    "content": f"Fallback action selected: {operation}|{target}|{value}",
                }
                tool_call_id = None

            if operation == "finalize" and not ready_to_finalize(observation):
                operation, target, value = pre_finalize_guard_action(observation)
                assistant_message = {
                    "role": "assistant",
                    "content": f"Guarded action selected before finalize: {operation}|{target}|{value}",
                }
                tool_call_id = None

            guarded_progression = progression_guard_action(observation, history, operation)
            if guarded_progression is not None:
                operation, target, value = guarded_progression
                assistant_message = {
                    "role": "assistant",
                    "content": f"Progression guard action selected: {operation}|{target}|{value}",
                }
                tool_call_id = None

            try:
                observation, reward, done, error = step_env(
                    session,
                    operation=operation,
                    target=target,
                    value=value,
                )
            except Exception as exc:
                reward = -0.25
                done = True
                error = str(exc)
                # Keep the previous observation to preserve end-state safety.

            rewards.append(reward)
            steps_taken = step

            action_text = f"{operation}|{target}|{value}"
            log_step(step=step, action=action_text, reward=reward, done=done, error=error)
            if INFERENCE_VERBOSE:
                log_detail(
                    step=step,
                    action=action_text,
                    observation=observation,
                    reward=reward,
                    done=done,
                    error=error,
                )
            history.append(f"{action_text} -> reward {reward:+.2f}")

            messages.append(assistant_message)
            tool_result = format_obs_for_llm(observation, step)
            if tool_call_id:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": tool_result,
                    }
                )
            else:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Observation update:\n{tool_result}\n\n"
                            "Choose one tool call for the next action."
                        ),
                    }
                )
            messages = trim_messages(messages)

            if done:
                break

        if observation is not None:
            score = float(observation.final_score)
            resolved = bool(observation.incident_resolved)
        success = resolved and score >= SUCCESS_SCORE_THRESHOLD
    finally:
        log_end(success=success, steps=steps_taken, score=score, resolved=resolved, rewards=rewards)
        if INFERENCE_VERBOSE:
            print(
                "[DETAIL] "
                f"task={task_name} success={str(success).lower()} steps={steps_taken} "
                f"final_score={score:.3f} resolved={str(resolved).lower()}",
                flush=True,
            )

    return task_name, success, steps_taken, score


def main() -> None:
    if not API_KEY:
        raise RuntimeError("Missing HF_TOKEN or OPENAI_API_KEY for OpenAI client authentication.")

    from openai import OpenAI

    client = OpenAI(base_url=API_BASE_URL, api_key=API_KEY)

    with requests.Session() as session:
        session.headers.update({"Accept": "application/json"})
        task_scores: List[Tuple[str, float, bool]] = []
        for fallback_task_name in TASK_ORDER:
            task_name, success, _steps, score = run_task(client, session, fallback_task_name)
            task_scores.append((task_name, score, success))
