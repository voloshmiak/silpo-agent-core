"""Runtime configuration for the SilpoFit agent."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

MCP_URL = "https://mcp.silpo.ua/mcp"
MODEL = "gemini-3.8-flash"

# A full pipeline — profile, cart context, searches, product details, the
# nutrition/budget/day checks and their corrections — lands around 30 steps,
# and a run that has to re-price a cart or redo a day needs the headroom on
# top of that. This only bounds a run that is going nowhere; the real ceiling
# on a healthy run is the Cloud Run request timeout (--timeout in deploy.yml).
MAX_STEPS = 60

@dataclass
class Settings:
    api_key: str
    model: str = MODEL
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
        return cls(api_key=api_key, service_tokens=tokens, **overrides)
