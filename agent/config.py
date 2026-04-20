"""Runtime configuration for the inference baseline."""

import os

from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("ENV_BASE_URL", "http://localhost:8000")
API_KEY = os.getenv("HF_TOKEN") or os.getenv("OPENAI_API_KEY") or os.getenv("API_KEY")
API_BASE_URL = os.getenv("API_BASE_URL") or "https://router.huggingface.co/v1"
MODEL_NAME = os.getenv("MODEL_NAME")
BENCHMARK = os.getenv("META_HACKATHON_BENCHMARK", "meta_hackathon")

MAX_STEPS = 16
TEMPERATURE = 0.1
MAX_TOKENS = 128
SUCCESS_SCORE_THRESHOLD = float(os.getenv("SUCCESS_SCORE_THRESHOLD", "0.20"))
_TASK_MODE = os.getenv("META_HACKATHON_TASK_MODE", "").strip().lower()
_ALL_TASKS = ["easy", "flaky", "medium", "network", "security", "hard"]
TASK_ORDER = [_TASK_MODE] if _TASK_MODE and _TASK_MODE in _ALL_TASKS else _ALL_TASKS

RESCUE_ON_NEGATIVE_REWARD = os.getenv("RESCUE_ON_NEGATIVE_REWARD", "false").lower() == "true"
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))
MESSAGE_WINDOW = 6
MAX_MODEL_CALLS_PER_TASK = int(os.getenv("MAX_MODEL_CALLS_PER_TASK", str(MAX_STEPS)))
PREFER_DETERMINISTIC_ACTIONS = os.getenv("PREFER_DETERMINISTIC_ACTIONS", "false").lower() == "true"
MAX_CONSECUTIVE_TOOL_CALL_MISSES = max(1, int(os.getenv("MAX_CONSECUTIVE_TOOL_CALL_MISSES", "4")))
MIN_MODEL_CALLS_BEFORE_FORCED_FALLBACK = max(
    1,
    int(os.getenv("MIN_MODEL_CALLS_BEFORE_FORCED_FALLBACK", "4")),
)

INFERENCE_VERBOSE = os.getenv("INFERENCE_VERBOSE", "false").strip().lower() == "true"
INFERENCE_DETAIL_MAX_ITEMS = max(1, int(os.getenv("INFERENCE_DETAIL_MAX_ITEMS", "3")))

