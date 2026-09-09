import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

MCP_URL = "https://mcp.silpo.ua/mcp"
MODEL = "gemini-3.5-flash-lite"

THINKING_LEVEL = "MEDIUM"

MAX_STEPS = 60

@dataclass
class Settings:
    api_key: str
    model: str = MODEL
    thinking_level: str = THINKING_LEVEL
    mcp_url: str = MCP_URL
    max_steps: int = MAX_STEPS
    service_tokens: frozenset[str] = field(default_factory=frozenset)

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
        }
        env = {k: v for k, v in env.items() if v}
        return cls(api_key=api_key, service_tokens=tokens, **(env | overrides))
