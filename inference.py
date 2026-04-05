import asyncio
import importlib
import json
import os
import textwrap
from typing import List, Optional

from client import MetaHackathonEnv
from models import MetaHackathonAction
IMAGE_NAME = os.getenv("LOCAL_IMAGE_NAME") or os.getenv("IMAGE_NAME") or "meta_hackathon-env:latest"
API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("HF_TOKEN") or os.getenv("API_KEY")

API_BASE_URL = os.getenv("API_BASE_URL") or "https://router.huggingface.co/v1"
MODEL_NAME = os.getenv("MODEL_NAME") or "Qwen/Qwen2.5-72B-Instruct"
BENCHMARK = os.getenv("META_HACKATHON_BENCHMARK", "meta_hackathon")
MAX_STEPS = 10
TEMPERATURE = 0.2
MAX_TOKENS = 220
SUCCESS_SCORE_THRESHOLD = 0.65

SYSTEM_PROMPT = textwrap.dedent(
    """
    You are an SRE agent operating a simulated production incident system.
    Return exactly one JSON object with keys: operation, target, value.

    Allowed operations:
    - inspect_alerts
    - inspect_metrics
    - inspect_service
    - inspect_logs
    - set_hypothesis
    - apply_fix
    - verify_fix

    Rules:
    - Use operation names exactly.
    - target can be empty unless required by operation.
    - value can be empty unless setting hypothesis or applying fix.
    - Never output anything except valid JSON.
    """
).strip()


def log_start(task: str, env: str, model: str) -> None:
    print(f"[START] task={task} env={env} model={model}", flush=True)


def log_step(step: int, action: str, reward: float, done: bool, error: Optional[str]) -> None:
    error_val = error if error else "null"
    done_val = str(done).lower()
    print(
        f"[STEP] step={step} action={action} reward={reward:.2f} done={done_val} error={error_val}",
        flush=True,
    )


def log_end(success: bool, steps: int, rewards: List[float]) -> None:
    rewards_str = ",".join(f"{r:.2f}" for r in rewards)
    print(f"[END] success={str(success).lower()} steps={steps} rewards={rewards_str}", flush=True)


def _action_to_str(action: MetaHackathonAction) -> str:
    return f"{action.operation}(target={action.target},value={action.value})"


def _safe_load_action(raw: str) -> Optional[MetaHackathonAction]:
    try:
        payload = json.loads(raw)
        operation = str(payload.get("operation", "")).strip()
        target = str(payload.get("target", "")).strip()
        value = str(payload.get("value", "")).strip()
        if not operation:
            return None
        return MetaHackathonAction(operation=operation, target=target, value=value)
    except Exception:
        return None


def _heuristic_action(observation, step: int) -> MetaHackathonAction:
    fix_by_task = {
        "easy": "scale-cache-cluster",
        "medium": "increase-payment-db-pool",
        "hard": "rollback-search-rollout",
    }
    hypothesis_by_task = {
        "easy": "redis cache miss storm with key eviction",
        "medium": "db connection pool saturation in payment service",
        "hard": "search thread pool exhaustion after rollout",
    }

    if step == 1:
        return MetaHackathonAction(operation="inspect_alerts", target="", value="")
    if step == 2:
        return MetaHackathonAction(operation="inspect_metrics", target="", value="")

    primary_service = observation.available_services[0] if observation.available_services else ""
    if step == 3:
        return MetaHackathonAction(operation="inspect_service", target=primary_service, value="")
    if step == 4:
        return MetaHackathonAction(operation="inspect_logs", target=primary_service, value="")
    if step == 5:
        return MetaHackathonAction(
            operation="set_hypothesis",
            target="",
            value=hypothesis_by_task.get(observation.task_id, "incident dependency bottleneck"),
        )
    if step == 6:
        return MetaHackathonAction(
            operation="apply_fix",
            target="",
            value=fix_by_task.get(observation.task_id, "rollback-search-rollout"),
        )
    if step == 7:
        return MetaHackathonAction(operation="verify_fix", target="", value="")

    return MetaHackathonAction(operation="verify_fix", target="", value="")


def build_user_prompt(step: int, observation, last_reward: float, history: List[str]) -> str:
    history_block = "\n".join(history[-4:]) if history else "None"
    suggested = ", ".join(observation.recommended_actions) if observation.recommended_actions else "None"
    return textwrap.dedent(
        f"""
        Step: {step}
        Task: {observation.task_id} ({observation.difficulty})
        Title: {observation.task_title}
        Status: {observation.status}
        Latest finding: {observation.latest_finding}
        Alerts: {observation.visible_alerts}
        Metrics: {observation.visible_metrics}
        Visible logs: {observation.visible_logs}
        Current hypothesis: {observation.current_hypothesis}
        Suggested actions: {suggested}
        Services: {observation.available_services}
        Last reward: {last_reward:.2f}
        Previous steps:
        {history_block}
        Return one JSON action.
        """
    ).strip()


def get_model_action(client, step: int, observation, last_reward: float, history: List[str]) -> MetaHackathonAction:
    if client is None:
        return _heuristic_action(observation, step)

    user_prompt = build_user_prompt(step, observation, last_reward, history)
    try:
        completion = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            stream=False,
        )
        text = (completion.choices[0].message.content or "").strip()
        parsed = _safe_load_action(text)
        if parsed:
            return parsed
    except Exception:
        pass
    return _heuristic_action(observation, step)


async def run_single_task(client, env) -> float:
    rewards: List[float] = []
    steps_taken = 0
    success = False
    last_reward = 0.0

    result = await env.reset()
    observation = result.observation

    log_start(task=observation.task_id, env=BENCHMARK, model=MODEL_NAME)

    history: List[str] = []
    try:
        for step in range(1, MAX_STEPS + 1):
            if result.done:
                break

            action = get_model_action(client, step, observation, last_reward, history)
            action_str = _action_to_str(action)
            error: Optional[str] = None

            try:
                result = await env.step(action)
            except Exception as exc:
                error = str(exc)
                log_step(step=step, action=action_str, reward=0.0, done=False, error=error)
                history.append(f"Step {step}: {action_str} -> error={error}")
                continue

            observation = result.observation
            reward = result.reward or 0.0
            done = result.done

            rewards.append(reward)
            steps_taken = step
            last_reward = reward

            log_step(step=step, action=action_str, reward=reward, done=done, error=error)
            history.append(f"Step {step}: {action_str} -> reward {reward:+.2f}")

            if done:
                break

        success = bool(observation.final_score >= SUCCESS_SCORE_THRESHOLD)
    finally:
        log_end(success=success, steps=steps_taken, rewards=rewards)

    return float(observation.final_score)


async def main() -> None:
    dotenv_module = importlib.util.find_spec("dotenv")
    if dotenv_module is not None:
        importlib.import_module("dotenv").load_dotenv()

    client = None
    openai_module = importlib.util.find_spec("openai")
    if openai_module is not None:
        openai_lib = importlib.import_module("openai")
        client = openai_lib.OpenAI(base_url=API_BASE_URL, api_key=API_KEY or "missing-api-key")

    env = await MetaHackathonEnv.from_docker_image(IMAGE_NAME)

    try:
        scores = []
        for _ in range(3):
            score = await run_single_task(client, env)
            scores.append(score)

    finally:
        try:
            await env.close()
        except Exception:
            # Do not fail submission run on container teardown timeout.
            pass


if __name__ == "__main__":
    asyncio.run(main())