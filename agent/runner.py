"""Orchestration loop for the agentic inference baseline."""

import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import requests

from .actions import (
    normalize_model_action,
    pre_finalize_guard_action,
    progression_guard_action,
    ready_to_finalize,
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
    SUCCESS_SCORE_THRESHOLD,
    TASK_ORDER,
    get_openai_client_kwargs,
)
from .http_environment import format_obs_for_llm, reset_env, step_env, trim_messages
from .model_client import get_model_action
from .prompts import build_system_prompt
from .trajectory_logging import log_detail, log_end, log_start, log_step

try:
    from ..models import MetaHackathonObservation
except ImportError:  # pragma: no cover - direct script execution
    from models import MetaHackathonObservation

try:
    from server.agent_memory import fingerprint, recall, remember
except ImportError:  # pragma: no cover - direct script execution
    try:
        from .server.agent_memory import fingerprint, recall, remember  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - memory is optional
        fingerprint = None  # type: ignore[assignment]
        recall = None  # type: ignore[assignment]
        remember = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from openai import OpenAI


def _normalize_hypothesis(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


# Maps error signatures → canonical fix phrases that fix_applier.py recognizes
_FIX_PHRASE_RULES: List[Tuple[List[str], str]] = [
    (["merge conflict", "<<<<<<<", "conflict marker", "<<<", "⚠ merge"], "resolve merge conflict markers"),
    (["requests", "urllib3", "dependency", "version conflict", "incompatible", "resolutionimpossible"], "pin compatible requests urllib3 versions"),
    (["dockerfile", "install order", "layer order", "copy before install"], "reorder dockerfile install steps"),
    (["flaky", "timing", "intermittent", "response_time", "test_response_time"], "add flaky test retry wrapper"),
    (["permission", "network", "compose network", "volume"], "fix docker compose network permission"),
    (["secret", "credential", "api_key", "hardcoded", "plaintext"], "remove hardcoded secrets from source"),
]


def _infer_fix_phrase(surfaced_errors: List[str], hypothesis: str = "", findings: List[str] | None = None) -> str:
    """Return a fix phrase matching the failure signature, or 'auto' for generic repair."""
    combined = " ".join(surfaced_errors + [hypothesis] + (findings or [])).lower()
    for keywords, phrase in _FIX_PHRASE_RULES:
        if any(kw in combined for kw in keywords):
            return phrase
    # Unknown fault type — signal the generic auto-repair strategy
    return "auto" if combined.strip() else ""


def _extract_primary_surfaced_error_file(observation: MetaHackathonObservation) -> str:
    for err in observation.surfaced_errors or []:
        text = str(err)
        match = re.search(r" in ([^:]+):\d+:", text)
        if match:
            return match.group(1).strip()
    return ""


def _select_fallback_action(
    observation: MetaHackathonObservation,
    action_history: List[Tuple[str, str, str]],
    fix_phrase: str = "",
    fix_applied_since_rerun: bool = False,
) -> Tuple[str, str, str]:
    surfaced_file = _extract_primary_surfaced_error_file(observation)
    stage = observation.current_stage or "build"

    candidates: List[Tuple[str, str, str]] = [
        ("inspect_config", surfaced_file or stage, ""),
        ("view_logs", stage, ""),
        ("inspect_dockerfile", "build", ""),
        ("inspect_permissions", stage, ""),
    ]

    # Inject fix candidates when we know what fix to apply
    if fix_phrase:
        is_dep_fix = any(kw in fix_phrase for kw in ["pin", "dependency", "urllib3", "requests"])
        fix_op = "add_dependency" if is_dep_fix else "modify_config"
        fix_candidate = (fix_op, stage, fix_phrase)
        if fix_candidate not in action_history:
            candidates.insert(0, fix_candidate)
        elif fix_phrase == "auto":
            # Retry auto-repair with fresh hint from latest stage
            alt = (fix_op, stage, f"auto-{len(action_history)}")
            candidates.insert(0, alt)

    candidates += [
        ("rerun_pipeline", "", ""),
        ("verify_fix", "", ""),
    ]

    for candidate in candidates:
        # Allow rerun_pipeline if a new fix was applied since the last one
        if candidate[0] == "rerun_pipeline" and fix_applied_since_rerun:
            return candidate
        if candidate not in action_history:
            return candidate

    return ("view_logs", stage, f"detail-{len(action_history) + 1}")


def _memory_hint(errors: List[str]) -> str:
    if not errors or recall is None:
        return ""
    suggestion = recall(errors)
    if not suggestion.get("suggested_fix"):
        return ""
    confidence = float(suggestion.get("confidence", 0.0) or 0.0)
    times_seen = int(suggestion.get("times_seen", 0) or 0)
    fix_text = str(suggestion.get("suggested_fix", "")).strip()
    return (
        "Persistent memory hint from prior episodes:\n"
        f"- confidence: {confidence:.3f}\n"
        f"- times_seen: {times_seen}\n"
        f"- suggested_fix: {fix_text}"
    )


def run_task(client: "OpenAI", session: requests.Session, fallback_task_name: str) -> Tuple[str, bool, int, float]:
    history: List[str] = []
    rewards: List[float] = []
    action_history: List[Tuple[str, str, str]] = []
    attempted_hypotheses: set[str] = set()
    forced_messages: List[str] = []
    inspected_config_targets: set[str] = set()
    injected_guardrails: set[str] = set()  # deduplicate guardrail messages
    fix_applied_since_rerun: bool = False   # allow rerun after a new fix
    fix_phrase: str = ""                    # canonical phrase inferred from errors
    hypothesis_accepted: bool = False       # True once set_hypothesis scores > 0
    steps_taken = 0
    score = 0.0
    success = False
    resolved = False
    task_name = fallback_task_name
    tool_call_misses = 0
    model_calls_used = 0
    disable_model_calls = MAX_MODEL_CALLS_PER_TASK <= 0
    observation: Optional[MetaHackathonObservation] = None
    messages: List[Dict[str, Any]] = []
    task_max_steps = MAX_STEPS
    initial_surfaced_errors: List[str] = []
    last_fix_value: str = ""
    last_memory_key: str = ""

    try:
        observation = reset_env(session)
        observed = observation.metadata or {}
        if isinstance(observed, dict) and observed.get("task_key"):
            task_name = str(observed.get("task_key"))
        if isinstance(observed, dict) and observed.get("max_steps"):
            task_max_steps = max(task_max_steps, int(observed.get("max_steps", MAX_STEPS)))
        initial_surfaced_errors = [str(item) for item in (observation.surfaced_errors or [])]
        last_memory_key = fingerprint(initial_surfaced_errors) if fingerprint is not None else ""
        fix_phrase = _infer_fix_phrase(initial_surfaced_errors, findings=list(observation.findings or []))

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
        memory_hint = _memory_hint(initial_surfaced_errors)
        task_intro = f"Task: {task_title}\n\n{format_obs_for_llm(observation, 0)}"
        if memory_hint:
            task_intro += f"\n\n{memory_hint}"
        messages.append(
            {
                "role": "user",
                "content": task_intro + "\n\nBegin debugging.",
            }
        )

        for step in range(1, task_max_steps + 1):
            if observation.done:
                break

            if forced_messages:
                for reminder in forced_messages:
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Guardrail: {reminder}",
                        }
                    )
                forced_messages.clear()
                messages = trim_messages(messages)

            operation = ""
            target = ""
            value = ""
            assistant_message: Dict[str, Any] = {
                "role": "assistant",
                "content": "",
            }
            tool_call_id: Optional[str] = None

            guard_attempts = 0
            while True:
                guard_attempts += 1

                if disable_model_calls:
                    assistant_message = {
                        "role": "assistant",
                        "content": "Model tool-calling disabled after repeated misses; using deterministic fallback.",
                    }
                    tool_call_id = None
                    operation, target, value = _select_fallback_action(
                        observation, action_history, fix_phrase, fix_applied_since_rerun
                    )
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

                should_resample = False
                surfaced_file = _extract_primary_surfaced_error_file(observation)

                # Inject surfaced-file guardrail at most once per unique file
                _sf_key = f"sf:{surfaced_file}"
                if (
                    surfaced_file
                    and surfaced_file not in inspected_config_targets
                    and (operation != "inspect_config" or target != surfaced_file)
                    and _sf_key not in injected_guardrails
                ):
                    injected_guardrails.add(_sf_key)
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "surfaced_errors names a primary file. Your next action must be "
                                f"inspect_config on '{surfaced_file}' before any other operation."
                            ),
                        }
                    )
                    should_resample = True

                _hyp_sf_key = f"hyp_sf:{surfaced_file}"
                if (
                    operation == "set_hypothesis"
                    and surfaced_file
                    and surfaced_file not in inspected_config_targets
                    and _hyp_sf_key not in injected_guardrails
                ):
                    injected_guardrails.add(_hyp_sf_key)
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Before set_hypothesis, call inspect_config on the surfaced_errors file "
                                f"'{surfaced_file}'."
                            ),
                        }
                    )
                    should_resample = True

                if operation == "set_hypothesis":
                    normalized_hypothesis = _normalize_hypothesis(value)
                    if normalized_hypothesis and normalized_hypothesis in attempted_hypotheses:
                        _dup_hyp_key = f"dup_hyp:{normalized_hypothesis}"
                        if _dup_hyp_key not in injected_guardrails:
                            injected_guardrails.add(_dup_hyp_key)
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        "You already tried this exact hypothesis and it scored negatively. "
                                        "Choose a different root cause and different hypothesis text."
                                    ),
                                }
                            )
                        should_resample = True

                action_tuple = (operation, target, value)
                # Allow rerun_pipeline again if a new fix was applied since the last rerun
                rerun_blocked = (
                    action_tuple in action_history
                    and not (operation == "rerun_pipeline" and fix_applied_since_rerun)
                )
                if rerun_blocked:
                    _dup_act_key = f"dup_act:{operation}:{target}"
                    if _dup_act_key not in injected_guardrails:
                        injected_guardrails.add(_dup_act_key)
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "You already tried this exact action and it failed. "
                                    "Choose a different operation, target, or value."
                                ),
                            }
                        )
                    should_resample = True

                if should_resample and disable_model_calls:
                    operation, target, value = _select_fallback_action(
                        observation, action_history, fix_phrase, fix_applied_since_rerun
                    )
                    action_tuple = (operation, target, value)
                    should_resample = False

                if should_resample and (not disable_model_calls) and guard_attempts < 4:
                    messages = trim_messages(messages)
                    continue

                break

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
            llm_thought = assistant_message.get("content", "") if assistant_message else ""
            if operation in {"modify_config", "add_dependency"} and value:
                last_fix_value = value
            
            action_text = f"{operation}|{target}|{value}"
            log_step(step=step, action=action_text, reward=reward, done=done, error=error, llm_thought=llm_thought)
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
            action_history.append((operation, target, value))
            if operation == "inspect_config" and target:
                inspected_config_targets.add(target)
                # Re-enable surfaced-file guardrail if a new file appears later
                injected_guardrails.discard(f"sf:{target}")
                injected_guardrails.discard(f"hyp_sf:{target}")
            if operation in {"modify_config", "add_dependency"} and value:
                fix_applied_since_rerun = True
            if operation == "rerun_pipeline":
                fix_applied_since_rerun = False
            if operation == "set_hypothesis":
                normalized_hypothesis = _normalize_hypothesis(value)
                if normalized_hypothesis:
                    attempted_hypotheses.add(normalized_hypothesis)
                if reward > 0:
                    hypothesis_accepted = True
                    # Refresh fix phrase using the accepted hypothesis text + current errors
                    obs_errors = [str(e) for e in (observation.surfaced_errors or [])]
                    obs_findings = [str(f) for f in (observation.findings or [])]
                    inferred = _infer_fix_phrase(obs_errors, hypothesis=value, findings=obs_findings)
                    if inferred:
                        fix_phrase = inferred
                    if fix_phrase and f"fix_hint:{fix_phrase}" not in injected_guardrails:
                        injected_guardrails.add(f"fix_hint:{fix_phrase}")
                        is_dep = any(kw in fix_phrase for kw in ["pin", "dependency", "urllib3", "requests"])
                        fix_op = "add_dependency" if is_dep else "modify_config"
                        if fix_phrase == "auto":
                            forced_messages.append(
                                "Hypothesis accepted. The fault type is unknown — apply a structured JSON fix. "
                                "Call 'modify_config' with a JSON value like: "
                                '{"file": "<path>", "action": "replace", "old": "<broken code>", "new": "<fixed code>"}. '
                                "Use the file content you inspected to fill in the exact old/new strings."
                            )
                        else:
                            forced_messages.append(
                                f"Hypothesis accepted. Now apply the fix immediately. "
                                f"Call tool '{fix_op}' with value='{fix_phrase}'. "
                                f"Do not inspect or view_logs again — go straight to the fix."
                            )
                elif reward < 0:
                    forced_messages.append(
                        "Your last hypothesis was incorrect (negative reward). "
                        "You must NOT repeat it. Re-read surfaced_errors and form a new hypothesis "
                        "targeting a different file or root cause."
                    )

            messages.append(assistant_message)
            tool_result = format_obs_for_llm(observation, step)
            observation_errors = [str(item) for item in (observation.surfaced_errors or [])]
            # Refresh fix_phrase from latest observation if not yet determined
            if not fix_phrase or not hypothesis_accepted:
                obs_findings = [str(f) for f in (observation.findings or [])]
                inferred = _infer_fix_phrase(observation_errors, findings=obs_findings)
                if inferred:
                    fix_phrase = inferred
            current_memory_key = fingerprint(observation_errors) if fingerprint is not None else ""
            memory_hint = _memory_hint(observation_errors) if current_memory_key != last_memory_key else ""
            if memory_hint:
                tool_result = f"{tool_result}\n\n{memory_hint}"
                last_memory_key = current_memory_key
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
        if remember is not None and initial_surfaced_errors and last_fix_value:
            try:
                remember(initial_surfaced_errors, last_fix_value, success)
            except Exception:
                pass
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
        raise RuntimeError(
            "Missing API credentials. Set provider-specific key: "
            "HF_TOKEN (hf), OPENROUTER_API_KEY (openrouter), or GROQ_API_KEY (groq)."
        )

    from openai import OpenAI

    client = OpenAI(**get_openai_client_kwargs())

    with requests.Session() as session:
        session.headers.update({"Accept": "application/json"})
        task_scores: List[Tuple[str, float, bool]] = []
        for fallback_task_name in TASK_ORDER:
            task_name, success, _steps, score = run_task(client, session, fallback_task_name)
            task_scores.append((task_name, score, success))
