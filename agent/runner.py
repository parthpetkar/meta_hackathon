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
    MIN_MODEL_CALLS_BEFORE_STRICT_FAIL,
    MODEL_NAME,
    NUM_EPISODES,
    SUCCESS_SCORE_THRESHOLD,
    TASK_ORDER,
    get_openai_client_kwargs,
)
from .http_environment import format_obs_for_llm, reset_env, step_env, trim_messages
from .model_client import get_model_action
from .prompts import build_system_prompt
from .trajectory_logging import log_detail, log_end, log_memory, log_start, log_step

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


# Typed guardrail keys — prevents string collision bugs where two different
# guardrails accidentally share the same deduplication key.
from enum import Enum, auto

class _GK(Enum):
    """Guardrail key enum. Each variant is a distinct deduplication slot."""
    FIX_HINT        = auto()   # structured JSON fix hint after hypothesis accepted
    SURFACED_FILE   = auto()   # "inspect this file first" nudge
    HYP_SURFACED    = auto()   # "inspect before hypothesis" nudge
    DUP_HYPOTHESIS  = auto()   # duplicate hypothesis block
    DUP_ACTION      = auto()   # duplicate action block


def _gk(kind: _GK, qualifier: str = "") -> str:
    """Build a unique string key from a typed enum + optional qualifier."""
    return f"{kind.name}:{qualifier}"


# Structured JSON fix template injected as a hint after hypothesis is accepted.
_STRUCTURED_FIX_HINT = (
    "Hypothesis accepted. Now apply the fix using modify_config with a structured JSON value:\n"
    '{"file": "<path/to/file>", "action": "replace", "old": "<exact broken lines>", "new": "<fixed lines>"}\n'
    "Use the file content you already inspected to fill in the exact old/new strings. "
    "Do not inspect or view_logs again — go straight to the fix."
)


def _extract_primary_surfaced_error_file(observation: MetaHackathonObservation) -> str:
    patterns = [
        r" in ([^:]+):\d+:",
        r"\b((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|ya?ml|txt|env))\b",
        r"\b(Dockerfile)\b",
    ]
    for err in observation.surfaced_errors or []:
        text = str(err)
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip().strip(".,)")
    return ""


def _memory_hint(errors: List[str], fault_type: str) -> Tuple[str, str]:
    if not errors or recall is None:
        return "", ""
    suggestion = recall(errors, fault_type=fault_type)
    if not suggestion.get("suggested_fix"):
        return "", ""
    confidence = float(suggestion.get("confidence", 0.0) or 0.0)
    times_seen = int(suggestion.get("times_seen", 0) or 0)
    fix_text = str(suggestion.get("suggested_fix", "")).strip()
    hint = (
        "Persistent memory hint from prior episodes:\n"
        f"- confidence: {confidence:.3f}\n"
        f"- times_seen: {times_seen}\n"
        f"- suggested_fix: {fix_text}"
    )
    memory_log = str(suggestion.get("memory_log", "") or "")
    return hint, memory_log


def _fault_type_from_observation(observation: MetaHackathonObservation, fallback: str = "unknown") -> str:
    metadata = observation.metadata if isinstance(observation.metadata, dict) else {}
    if isinstance(metadata, dict) and metadata.get("fault_type"):
        return str(metadata.get("fault_type"))
    if observation.task_id and observation.task_id.startswith("real_"):
        return observation.task_id[len("real_") :]
    return fallback


def _repetition_escape_action(
    observation: MetaHackathonObservation,
    action_history: List[Tuple[str, str, str]],
    repeated_action: Tuple[str, str, str],
) -> Tuple[str, str, str]:
    stage = observation.current_stage or "build"
    repeated_text = "|".join(repeated_action).lower()

    candidates: List[Tuple[str, str, str]] = []
    if "requirements" in repeated_text or stage == "build":
        candidates.extend(
            [
                ("inspect_config", "services/api/requirements.txt", ""),
                ("inspect_config", "requirements.txt", ""),
                ("inspect_config", "Dockerfile", ""),
            ]
        )

    candidates.extend(
        [
            ("view_logs", stage, ""),
            ("inspect_config", "docker-compose.yml", ""),
            ("inspect_dockerfile", "build", ""),
        ]
    )

    for candidate in candidates:
        if candidate != repeated_action and candidate not in action_history:
            return candidate
    return ("view_logs", stage, "")


def run_task(client: "OpenAI", session: requests.Session, fallback_task_name: str) -> Tuple[str, bool, int, float]:
    history: List[str] = []
    rewards: List[float] = []
    action_history: List[Tuple[str, str, str]] = []
    attempted_hypotheses: set[str] = set()
    forced_messages: List[str] = []
    inspected_config_targets: set[str] = set()
    injected_guardrails: set[str] = set()  # deduplicate guardrail messages
    fix_applied_since_rerun: bool = False   # allow rerun after a new fix
    hypothesis_accepted: bool = False       # True once set_hypothesis scores > 0
    steps_taken = 0
    score = 0.0
    deterministic_score = 0.0
    rubric_score = 0.0
    rubric_judge_used = False
    success = False
    resolved = False
    task_name = fallback_task_name
    tool_call_misses = 0
    model_calls_used = 0
    observation: Optional[MetaHackathonObservation] = None
    messages: List[Dict[str, Any]] = []
    task_max_steps = MAX_STEPS
    initial_surfaced_errors: List[str] = []
    last_fix_value: str = ""
    errors_at_last_fix: List[str] = []   # errors active when the last fix was applied
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
        fault_type = _fault_type_from_observation(observation, task_name)
        _meta = observation.metadata if isinstance(observation.metadata, dict) else {}
        print(
            f"[EPISODE START] label={fallback_task_name}  fault={fault_type}  "
            f"stage={_meta.get('expected_fail_stage', '?')}  "
            f"difficulty={observation.difficulty or '?'}  "
            f"max_steps={task_max_steps}",
            flush=True,
        )
        memory_hint, memory_log = _memory_hint(initial_surfaced_errors, fault_type)
        if memory_log:
            log_memory(memory_log)
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

            _model_failed = False
            guard_attempts = 0
            try:
              while True:
                guard_attempts += 1

                operation, target, value, assistant_message, tool_call_id = get_model_action(
                    client=client,
                    step=step,
                    messages=messages,
                )
                model_calls_used += 1
                if model_calls_used > MAX_MODEL_CALLS_PER_TASK:
                    raise RuntimeError(
                        "Model call budget exceeded while strict tool-calling is enabled. "
                        "Increase MAX_MODEL_CALLS_PER_TASK or use a model/provider with reliable tool calls."
                    )

                if tool_call_id is None:
                    tool_call_misses += 1
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your previous response did not include a native tool call. "
                                "Return exactly one valid tool call from the provided tool schema."
                            ),
                        }
                    )
                    if (
                        tool_call_misses >= MAX_CONSECUTIVE_TOOL_CALL_MISSES
                        and model_calls_used >= MIN_MODEL_CALLS_BEFORE_STRICT_FAIL
                    ):
                        raise RuntimeError(
                            "Model failed to emit required tool calls repeatedly. "
                            "Strict tool-calling is mandatory and fallback is disabled."
                        )
                    if guard_attempts < 4:
                        messages = trim_messages(messages)
                        continue
                    raise RuntimeError(
                        "Model response missing tool call after retries in strict mode."
                    )
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
                _sf_key = _gk(_GK.SURFACED_FILE, surfaced_file)
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

                _hyp_sf_key = _gk(_GK.HYP_SURFACED, surfaced_file)
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
                        _dup_hyp_key = _gk(_GK.DUP_HYPOTHESIS, normalized_hypothesis)
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
                    _dup_act_key = _gk(_GK.DUP_ACTION, f"{operation}:{target}")
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

                if should_resample and guard_attempts < 4:
                    messages = trim_messages(messages)
                    continue

                break
            except RuntimeError as _model_exc:
                _model_failed = True
                log_step(
                    step=step,
                    action="[model_failure]||",
                    reward=0.0,
                    done=True,
                    error=str(_model_exc),
                    llm_thought="",
                )

            if _model_failed:
                break

            action_tuple = (operation, target, value)
            if action_history.count(action_tuple) >= 3:
                operation, target, value = _repetition_escape_action(observation, action_history, action_tuple)
                assistant_message = {
                    "role": "assistant",
                    "content": (
                        "Repetition guard triggered: forcing a different action after 3+ identical attempts "
                        f"-> {operation}|{target}|{value}"
                    ),
                }
                tool_call_id = None

            # Capture errors active RIGHT BEFORE this fix so memory maps
            # the error pattern that prompted the fix, not the initial errors.
            pre_fix_errors: List[str] = []
            if operation in {"modify_config", "add_dependency"} and value:
                pre_fix_errors = [str(e) for e in (observation.surfaced_errors or [])]

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
                if pre_fix_errors:
                    errors_at_last_fix = pre_fix_errors
            
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
                injected_guardrails.discard(_gk(_GK.SURFACED_FILE, target))
                injected_guardrails.discard(_gk(_GK.HYP_SURFACED, target))
            if operation in {"modify_config", "add_dependency"} and value:
                fix_applied_since_rerun = True
                # After any fix, push the agent to rerun immediately so it sees the
                # updated error state. Without this nudge, agents often inspect stale
                # errors and exhaust the budget before verifying the fix.
                fix_note = str(observation.findings[-1]) if observation.findings else ""
                fix_succeeded = "Fix applied" in fix_note or reward >= 0.0
                if fix_succeeded and not observation.incident_resolved and not done:
                    forced_messages.append(
                        "Fix applied. Call rerun_pipeline NOW to see whether the issue is resolved. "
                        "Do not inspect files or form hypotheses until you have seen the new pipeline state."
                    )
            if operation == "rerun_pipeline":
                fix_applied_since_rerun = False
            if operation == "set_hypothesis":
                normalized_hypothesis = _normalize_hypothesis(value)
                if normalized_hypothesis:
                    attempted_hypotheses.add(normalized_hypothesis)
                if reward > 0:
                    hypothesis_accepted = True
                    if _gk(_GK.FIX_HINT) not in injected_guardrails:
                        injected_guardrails.add(_gk(_GK.FIX_HINT))
                        forced_messages.append(_STRUCTURED_FIX_HINT)
                elif reward < 0:
                    forced_messages.append(
                        "Your last hypothesis was incorrect (negative reward). "
                        "You must NOT repeat it. Re-read surfaced_errors and form a new hypothesis "
                        "targeting a different file or root cause."
                    )

            messages.append(assistant_message)
            tool_result = format_obs_for_llm(observation, step)
            observation_errors = [str(item) for item in (observation.surfaced_errors or [])]
            current_memory_key = fingerprint(observation_errors) if fingerprint is not None else ""
            observation_fault_type = _fault_type_from_observation(observation, task_name)
            memory_hint, memory_log = (
                _memory_hint(observation_errors, observation_fault_type)
                if current_memory_key != last_memory_key
                else ("", "")
            )
            if memory_log:
                log_memory(memory_log)
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

        # Runner exhausted step budget without agent calling finalize — force one to trigger scoring.
        if observation is not None and not observation.done:
            try:
                observation, reward, done, error = step_env(
                    session,
                    operation="finalize",
                    target="",
                    value="",
                )
                rewards.append(reward)
                log_step(
                    step=steps_taken + 1,
                    action="finalize||",
                    reward=reward,
                    done=done,
                    error=error,
                    llm_thought="[forced finalize: step budget exhausted]",
                )
            except Exception as exc:
                pass  # best-effort; scoring may be partial

        if observation is not None:
            score = float(observation.final_score)
            deterministic_score = float(observation.deterministic_score)
            rubric_score = float(observation.rubric_score)
            rubric_judge_used = bool(observation.rubric_judge_used)
            resolved = bool(observation.incident_resolved)
        success = resolved and score >= SUCCESS_SCORE_THRESHOLD
        # Use the errors that were active when the fix was applied, not the initial errors.
        # This prevents multi-fault episodes from poisoning the memory: e.g. a dep_conflict
        # episode that also had log_path would previously store "dep errors → log_path fix",
        # teaching the wrong fix for dep errors in future episodes.
        memory_errors = errors_at_last_fix if errors_at_last_fix else initial_surfaced_errors
        if remember is not None and memory_errors and last_fix_value:
            try:
                remember(memory_errors, last_fix_value, success)
            except Exception:
                pass
    except Exception as _episode_exc:
        import traceback as _tb
        print(
            f"\n[EPISODE ERROR] {type(_episode_exc).__name__}: {_episode_exc}",
            flush=True,
        )
        _tb.print_exc()
        raise
    finally:
        log_end(
            success=success,
            steps=steps_taken,
            score=score,
            resolved=resolved,
            rewards=rewards,
            deterministic_score=deterministic_score,
            rubric_score=rubric_score,
            rubric_judge_used=rubric_judge_used,
        )
        if INFERENCE_VERBOSE:
            print(
                "[DETAIL] "
                f"task={task_name} success={str(success).lower()} steps={steps_taken} "
                f"final_score={score:.3f} det={deterministic_score:.3f} "
                f"rubric={rubric_score:.3f} judge={str(rubric_judge_used).lower()} "
                f"resolved={str(resolved).lower()}",
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
        print(f"[INFERENCE] Running {NUM_EPISODES} episodes — fault seed selected per-episode by curriculum UCB1 (based on prior scores), scenario composed by LLM adversarial designer.", flush=True)
        for episode_label in TASK_ORDER:
            # episode_label is just a slot name ("episode_1", etc.).
            # The actual fault/scenario is generated fresh by LLM for each reset.
            task_name, success, _steps, score = run_task(client, session, episode_label)
            task_scores.append((task_name, score, success))
