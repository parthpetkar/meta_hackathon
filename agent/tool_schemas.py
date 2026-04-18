"""Tool-call schema definitions used by the inference LLM."""

from typing import Any, Dict, List, Set

VALID_OPERATIONS: Set[str] = {
    "view_logs",
    "inspect_config",
    "inspect_dockerfile",
    "modify_config",
    "add_dependency",
    "rerun_pipeline",
    "verify_fix",
    "finalize",
    "inspect_permissions",
    "set_hypothesis",
}

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "view_logs",
            "description": "Read pipeline/runtime logs for the active failure context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "stage": {
                        "type": "string",
                        "enum": ["build", "test", "deploy"],
                        "description": "Pipeline stage to inspect",
                    },
                    "detail": {
                        "type": "string",
                        "description": "Optional detail filter",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_config",
            "description": "Inspect CI/deploy config clues and surfaced config files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "component": {
                        "type": "string",
                        "description": "Stage or component to inspect",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_dockerfile",
            "description": "Inspect Dockerfile and security build clues.",
            "parameters": {
                "type": "object",
                "properties": {
                    "component": {
                        "type": "string",
                        "description": "Component to inspect",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_permissions",
            "description": "Inspect IAM and service-account permission clues.",
            "parameters": {
                "type": "object",
                "properties": {
                    "component": {
                        "type": "string",
                        "description": "Component to inspect",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_hypothesis",
            "description": "Declare your current root-cause hypothesis before attempting fixes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hypothesis": {
                        "type": "string",
                        "description": "Root cause hypothesis text",
                    }
                },
                "required": ["hypothesis"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_config",
            "description": "Apply a config, deploy, rollback, or security fix candidate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "component": {
                        "type": "string",
                        "description": "Stage or component to fix",
                    },
                    "fix": {
                        "type": "string",
                        "description": "The fix to apply",
                    },
                },
                "required": ["fix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_dependency",
            "description": "Apply a dependency pin or compatibility fix.",
            "parameters": {
                "type": "object",
                "properties": {
                    "component": {
                        "type": "string",
                        "description": "Stage or component",
                    },
                    "dependency_fix": {
                        "type": "string",
                        "description": "Dependency fix to apply",
                    },
                },
                "required": ["dependency_fix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rerun_pipeline",
            "description": "Re-run the pipeline after fix attempts to validate progression.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_fix",
            "description": "Confirm that the latest rerun removed the target failure before finalization.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize",
            "description": "End the episode and request final scoring.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

