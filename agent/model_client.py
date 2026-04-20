"""LLM tool-call translation for the inference runner."""

import json
from typing import Any, Dict, Optional, Tuple

from .config import MAX_TOKENS, MODEL_NAME, TEMPERATURE
from .tool_schemas import TOOL_SCHEMAS


def _parse_tool_arguments(arguments: Any) -> Dict[str, Any]:
    if isinstance(arguments, dict):
        return dict(arguments)
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def _tool_call_to_action_parts(tool_name: str, tool_args: Dict[str, Any]) -> Tuple[str, str, str]:
    name = (tool_name or "").strip()
    if name == "view_logs":
        stage = str(tool_args.get("stage", "") or "")
        detail = str(tool_args.get("detail", "") or "")
        return "view_logs", stage, detail
    if name == "inspect_config":
        return "inspect_config", str(tool_args.get("component", "") or ""), ""
    if name == "inspect_dockerfile":
        return "inspect_dockerfile", str(tool_args.get("component", "") or ""), ""
    if name == "inspect_permissions":
        return "inspect_permissions", str(tool_args.get("component", "") or ""), ""
    if name == "set_hypothesis":
        return "set_hypothesis", "", str(tool_args.get("hypothesis", "") or "")
    if name == "modify_config":
        return (
            "modify_config",
            str(tool_args.get("component", "") or ""),
            str(tool_args.get("fix", "") or ""),
        )
    if name == "add_dependency":
        return (
            "add_dependency",
            str(tool_args.get("component", "") or ""),
            str(tool_args.get("dependency_fix", "") or ""),
        )
    if name == "rerun_pipeline":
        return "rerun_pipeline", "", ""
    if name == "verify_fix":
        return "verify_fix", "", ""
    if name == "finalize":
        return "finalize", "", ""
    return name.lower(), "", ""


def get_model_action(
    client: Any,
    step: int,
    messages: list[Dict[str, Any]],
) -> Tuple[str, str, str, Dict[str, Any], Optional[str]]:
    completion = client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        tools=TOOL_SCHEMAS,
        tool_choice="auto",
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        stream=False,
    )

    message = completion.choices[0].message
    if message.tool_calls:
        tool_call = message.tool_calls[0]
        tool_name = (tool_call.function.name or "").strip()
        tool_args = _parse_tool_arguments(tool_call.function.arguments)
        operation, target, value = _tool_call_to_action_parts(tool_name, tool_args)
        tool_call_id = tool_call.id or f"call_{step}"
        assistant_message: Dict[str, Any] = {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_args, ensure_ascii=True, separators=(",", ":")),
                    },
                }
            ],
        }
        return operation, target, value, assistant_message, tool_call_id

    text = (message.content or "").strip()
    assistant_message = {
        "role": "assistant",
        "content": text,
    }
    # Strict mode: require native tool call structure.
    return "", "", "", assistant_message, None
