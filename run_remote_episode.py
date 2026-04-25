#!/usr/bin/env python3
"""Run a simple OpenEnv episode flow against a remote HTTP server.

Flow:
1) POST /reset
2) GET /state
3) POST /step
4) GET /state
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional

import requests


def _pick(data: Dict[str, Any], path: str) -> Any:
    current: Any = data
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _print_summary(label: str, status_code: int, payload: Dict[str, Any]) -> None:
    observation = payload.get("observation", {}) if isinstance(payload, dict) else {}
    state = payload.get("state", {}) if isinstance(payload, dict) else {}

    summary = {
        "done": payload.get("done"),
        "reward": payload.get("reward"),
        "task_id": observation.get("task_id"),
        "pipeline_status": observation.get("pipeline_status"),
        "current_stage": observation.get("current_stage"),
        "state_step_count": state.get("step_count"),
        "top_step_count": payload.get("step_count"),
        "episode_id": payload.get("episode_id"),
    }
    summary = {k: v for k, v in summary.items() if v is not None}

    print(f"--- {label} --- Status: {status_code}")
    if summary:
        print(json.dumps(summary, indent=2))
    else:
        print("{}")


def _request_json(
    session: requests.Session,
    method: str,
    url: str,
    body: Optional[Dict[str, Any]],
    timeout: float,
) -> Dict[str, Any]:
    response = session.request(method, url, json=body, timeout=timeout)
    response.raise_for_status()
    return response.json()


def run_episode(
    base_url: str,
    operation: str,
    target: str,
    value: str,
    timeout: float,
    wrap_action: bool,
) -> int:
    base_url = base_url.rstrip("/")

    with requests.Session() as session:
        try:
            reset_payload = _request_json(
                session,
                "POST",
                f"{base_url}/reset",
                {},
                timeout,
            )
            _print_summary("POST /reset", 200, reset_payload)

            state_before = _request_json(
                session,
                "GET",
                f"{base_url}/state",
                None,
                timeout,
            )
            _print_summary("GET /state", 200, state_before)

            action = {"operation": operation, "target": target, "value": value}
            step_body = {"action": action} if wrap_action else action
            step_payload = _request_json(
                session,
                "POST",
                f"{base_url}/step",
                step_body,
                timeout,
            )
            _print_summary("POST /step", 200, step_payload)

            state_after = _request_json(
                session,
                "GET",
                f"{base_url}/state",
                None,
                timeout,
            )
            _print_summary("GET /state", 200, state_after)
            return 0

        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            body_text = exc.response.text if exc.response is not None else ""
            print(f"HTTP error during episode run: status={status}")
            if body_text:
                print(body_text)
            return 1
        except requests.RequestException as exc:
            print(f"Request error during episode run: {exc}")
            return 1
        except json.JSONDecodeError as exc:
            print(f"Response was not valid JSON: {exc}")
            return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run reset/state/step/state sequence against a remote OpenEnv server."
    )
    parser.add_argument(
        "--base-url",
        default="https://parthpetkar-metahackathon.hf.space",
        help="Base URL for the environment server.",
    )
    parser.add_argument(
        "--operation",
        default="view_logs",
        help="Action operation sent in /step.",
    )
    parser.add_argument(
        "--target",
        default="build",
        help="Action target sent in /step.",
    )
    parser.add_argument(
        "--value",
        default="",
        help="Action value sent in /step.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--raw-action",
        action="store_true",
        help="Send action payload directly to /step instead of wrapping it under {'action': ...}.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run_episode(
        base_url=args.base_url,
        operation=args.operation,
        target=args.target,
        value=args.value,
        timeout=args.timeout,
        wrap_action=not args.raw_action,
    )


if __name__ == "__main__":
    sys.exit(main())
