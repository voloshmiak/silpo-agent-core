import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

MCP_URL = "https://mcp.silpo.ua/mcp"
MODEL = "gemini-3.5-flash-lite"

THINKING_LEVEL = "MEDIUM"

MAX_STEPS = 60

VALIDATION_ROUNDS = 2

@dataclass
class Settings:
    api_key: str
    model: str = MODEL
    thinking_level: str = THINKING_LEVEL
    mcp_url: str = MCP_URL
    max_steps: int = MAX_STEPS
    validation_rounds: int = VALIDATION_ROUNDS
    validator_model: str = ""
    validator_thinking_level: str = ""
    service_tokens: frozenset[str] = field(default_factory=frozenset)

    @property
    def review_model(self) -> str:
        return self.validator_model or self.model

    @property
    def review_thinking_level(self) -> str:
        return self.validator_thinking_level or self.thinking_level

    @classmethod
    def load(cls, **overrides) -> "Settings":
        load_dotenv()
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set (put it in .env)")
        tokens = frozenset(
            t.strip() for t in os.getenv("SILPOFIT_SERVICE_TOKENS", "").split(",") if t.strip()
        )
        env = {
            "model": os.getenv("SILPOFIT_MODEL"),
            "thinking_level": os.getenv("SILPOFIT_THINKING_LEVEL"),
            "validator_model": os.getenv("SILPOFIT_VALIDATOR_MODEL"),
            "validator_thinking_level": os.getenv("SILPOFIT_VALIDATOR_THINKING_LEVEL"),
        }
        env = {k: v for k, v in env.items() if v}
        env["validation_rounds"] = _int_env("SILPOFIT_VALIDATION_ROUNDS", VALIDATION_ROUNDS)
        return cls(api_key=api_key, service_tokens=tokens, **(env | overrides))


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a whole number, got {raw!r}") from None
