"""Runtime configuration for the SilpoFit agent."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

MCP_URL = "https://mcp.silpo.ua/mcp"
MODEL = "gemini-3.8-flash"

# Gemini 3 flash models think by default (MEDIUM), and this agent spends 30-60
# model turns per run — so the thinking tax is paid on every one of them. The
# reasoning that matters here (which product, which day, does the budget hold)
# is carried by the prompt and the local check tools, not by long deliberation
# inside a single turn, so the lowest rung keeps the planning quality and drops
# the wait. The ladder is MINIMAL < LOW < MEDIUM < HIGH; raise it via
# SILPOFIT_THINKING_LEVEL if plans degrade.
THINKING_LEVEL = "MINIMAL"

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
        # Model and thinking level come from the environment so a latency or
        # quality change can be tried on a deployed revision without a rebuild.
        env = {
            "model": os.getenv("SILPOFIT_MODEL"),
            "thinking_level": os.getenv("SILPOFIT_THINKING_LEVEL"),
        }
        env = {k: v for k, v in env.items() if v}
        return cls(api_key=api_key, service_tokens=tokens, **(env | overrides))
