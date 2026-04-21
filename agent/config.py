"""Runtime configuration for the inference baseline."""

import os

from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("ENV_BASE_URL", "http://localhost:8000")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "hf").strip().lower()


def _resolve_api_base_url() -> str:
    explicit = (os.getenv("API_BASE_URL") or "").strip()
    if explicit:
        return explicit
    if LLM_PROVIDER == "openrouter":
        return "https://openrouter.ai/api/v1"
    if LLM_PROVIDER == "groq":
        return "https://api.groq.com/openai/v1"
    return "https://router.huggingface.co/v1"


def _resolve_api_key() -> str:
    explicit = (os.getenv("API_KEY") or "").strip()
    if explicit:
        return explicit
    if LLM_PROVIDER == "openrouter":
        return (
            os.getenv("OPENROUTER_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("HF_TOKEN")
            or os.getenv("GROQ_API_KEY")
            or ""
        )
    if LLM_PROVIDER == "groq":
        return (
            os.getenv("GROQ_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("HF_TOKEN")
            or os.getenv("OPENROUTER_API_KEY")
            or ""
        )
    return (
        os.getenv("HF_TOKEN")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
        or os.getenv("GROQ_API_KEY")
        or ""
    )


API_BASE_URL = _resolve_api_base_url()
API_KEY = _resolve_api_key()
OPENROUTER_REFERER = (os.getenv("OPENROUTER_REFERER") or "").strip()
OPENROUTER_TITLE = (os.getenv("OPENROUTER_TITLE") or "meta_hackathon").strip()


def get_openai_client_kwargs() -> dict:
    kwargs = {
        "base_url": API_BASE_URL,
        "api_key": API_KEY,
    }
    is_openrouter = LLM_PROVIDER == "openrouter" or "openrouter.ai" in API_BASE_URL
    if is_openrouter:
        headers = {}
        if OPENROUTER_REFERER:
            headers["HTTP-Referer"] = OPENROUTER_REFERER
        if OPENROUTER_TITLE:
            headers["X-Title"] = OPENROUTER_TITLE
        if headers:
            kwargs["default_headers"] = headers
    return kwargs


MODEL_NAME = os.getenv("MODEL_NAME")
BENCHMARK = os.getenv("META_HACKATHON_BENCHMARK", "meta_hackathon")

MAX_STEPS = 16
TEMPERATURE = 0.1
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "512"))
SUCCESS_SCORE_THRESHOLD = float(os.getenv("SUCCESS_SCORE_THRESHOLD", "0.20"))
# Number of episodes to run per inference session.
# Fault selection is always by LLM adversarial designer.
# Difficulty is scheduled by curriculum (UCB1 + EMA).
NUM_EPISODES = int(os.getenv("META_HACKATHON_NUM_EPISODES", "6"))
TASK_ORDER = [f"episode_{i+1}" for i in range(NUM_EPISODES)]

RESCUE_ON_NEGATIVE_REWARD = os.getenv("RESCUE_ON_NEGATIVE_REWARD", "false").lower() == "true"
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))
MESSAGE_WINDOW = int(os.getenv("MESSAGE_WINDOW", "12"))
MAX_MODEL_CALLS_PER_TASK = int(os.getenv("MAX_MODEL_CALLS_PER_TASK", str(MAX_STEPS * 3)))
PREFER_DETERMINISTIC_ACTIONS = os.getenv("PREFER_DETERMINISTIC_ACTIONS", "false").lower() == "true"
MAX_CONSECUTIVE_TOOL_CALL_MISSES = max(1, int(os.getenv("MAX_CONSECUTIVE_TOOL_CALL_MISSES", "6")))
MIN_MODEL_CALLS_BEFORE_FORCED_FALLBACK = max(
    1,
    int(os.getenv("MIN_MODEL_CALLS_BEFORE_FORCED_FALLBACK", "4")),
)

INFERENCE_VERBOSE = os.getenv("INFERENCE_VERBOSE", "false").strip().lower() == "true"
INFERENCE_DETAIL_MAX_ITEMS = max(1, int(os.getenv("INFERENCE_DETAIL_MAX_ITEMS", "3")))

