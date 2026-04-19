"""Structured logging helpers for inference runs."""

from typing import Any, Dict, List, Optional

from .config import INFERENCE_DETAIL_MAX_ITEMS

try:
    from ..models import MetaHackathonObservation
except ImportError:  # pragma: no cover - direct script execution
    from models import MetaHackathonObservation


def log_start(task: str, env: str, model: str) -> None:
    print(f"[START] task={task} env={env} model={model}", flush=True)


def log_step(step: int, action: str, reward: float, done: bool, error: Optional[str]) -> None:
    error_val = error if error else "null"
    done_val = str(done).lower()
    print(
        f"[STEP] step={step} action={action} reward={reward:.2f} done={done_val} error={error_val}",
        flush=True,
    )


def log_end(success: bool, steps: int, score: float, resolved: bool, rewards: List[float]) -> None:
    rewards_str = ",".join(f"{r:.2f}" for r in rewards)
    print(
        f"[END] success={str(success).lower()} steps={steps} score={score:.3f} "
        f"resolved={str(resolved).lower()} rewards={rewards_str}",
        flush=True,
    )


def _compact_list(values: List[Any], limit: int = INFERENCE_DETAIL_MAX_ITEMS) -> str:
    if not values:
        return "none"
    compact = [str(item).replace("\n", " ").strip() for item in values[-limit:]]
    return " || ".join(compact)


def _compact_stage_map(stage_map: Dict[str, Any]) -> str:
    if not stage_map:
        return "unknown"
    return ",".join(f"{stage}:{status}" for stage, status in stage_map.items())


def log_detail(
    *,
    step: int,
    action: str,
    observation: MetaHackathonObservation,
    reward: float,
    done: bool,
    error: Optional[str],
) -> None:
    """Emit verbose trajectory diagnostics for local debugging without changing strict logs."""
    metadata = observation.metadata if isinstance(observation.metadata, dict) else {}
    error_val = error if error else "null"

    print(
        "[DETAIL] "
        f"step={step} action={action} stage={observation.current_stage or '?'} "
        f"status={observation.pipeline_status or '?'} issue_index={observation.active_issue_index} "
        f"revealed={observation.revealed_issue_count} health={observation.pipeline_health:.2f} "
        f"cost={observation.recovery_cost} redundant={observation.redundant_actions} "
        f"destructive={observation.destructive_actions} reward={reward:.2f} "
        f"done={str(done).lower()} error={error_val}",
        flush=True,
    )

    print(
        "[DETAIL] "
        f"stages={_compact_stage_map(observation.pipeline_stages)} "
        f"alerts={_compact_list(observation.visible_alerts)} "
        f"errors={_compact_list(observation.surfaced_errors)} "
        f"findings={_compact_list(observation.findings)}",
        flush=True,
    )

    if metadata.get("audit_enabled"):
        buckets = metadata.get("active_issue_pattern_buckets") or []
        if not isinstance(buckets, list):
            buckets = []

        events = metadata.get("sampled_pattern_events") or []
        event_preview: List[str] = []
        if isinstance(events, list):
            for event in events[:INFERENCE_DETAIL_MAX_ITEMS]:
                if isinstance(event, dict):
                    bucket = str(event.get("bucket", "?"))
                    line_index = event.get("line_index", "?")
                    event_preview.append(f"{bucket}[{line_index}]")

        print(
            "[DETAIL] "
            f"audit variant={metadata.get('variant_id', '?')} "
            f"seed={metadata.get('episode_seed', '?')} "
            f"buckets={','.join(str(bucket) for bucket in buckets) if buckets else 'none'} "
            f"events={metadata.get('sampled_pattern_event_count', 0)} "
            f"event_preview={','.join(event_preview) if event_preview else 'none'}",
            flush=True,
        )

