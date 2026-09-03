"""HTTP entry point.

SilpoFit runs as a stateless agent service: the calling backend owns user
authorization, profiles and history, and sends everything the agent needs in
one request. The agent holds nothing between requests — no tokens, no plans.
"""

from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .agent import AgentResult, SilpoFitAgent
from .config import Settings
from .mcp_client import SilpoMCP, SilpoTokenExpired
from .prompts import build_plan_prompt, build_review_prompt

settings = Settings.load()
app = FastAPI(
    title="SilpoFit Agent",
    version="0.1.0",
    description=(
        "Service-to-service API: the caller is a backend, not the end user. "
        "Use the Authorize button with a value from SILPOFIT_SERVICE_TOKENS — "
        "that authenticates the backend. The end user's own Silpo token goes "
        "separately, in the request body's `silpo_access_token` field."
    ),
)
_bearer = HTTPBearer(
    auto_error=False,
    description="Backend service token from SILPOFIT_SERVICE_TOKENS (not the user's Silpo token).",
)


def require_service_token(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Authenticates the calling backend, not the end user."""
    if not settings.service_tokens:
        raise HTTPException(500, "SILPOFIT_SERVICE_TOKENS is not configured")
    if creds is None or creds.credentials not in settings.service_tokens:
        raise HTTPException(401, "invalid or missing service token")


class Profile(BaseModel):
    weight_kg: float
    target_weight_kg: float
    height_cm: float | None = None
    age: int | None = None
    sex: Literal["male", "female"] | None = None


class PlanRequest(BaseModel):
    silpo_access_token: str
    profile: Profile
    budget_uah: float
    workouts_per_week: int = 0
    fridge_items: list[str] = Field(default_factory=list)
    note: str = ""
    previous_plan: dict[str, Any] | None = None
    apply: bool = False


class ReviewRequest(BaseModel):
    silpo_access_token: str
    previous_plan: dict[str, Any]


class AgentResponse(BaseModel):
    answer: str
    plan_to_persist: dict[str, Any] | None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/plan", dependencies=[Depends(require_service_token)], response_model=AgentResponse)
async def plan(body: PlanRequest) -> AgentResponse:
    prompt = build_plan_prompt(
        weight_kg=body.profile.weight_kg,
        target_weight_kg=body.profile.target_weight_kg,
        height_cm=body.profile.height_cm,
        age=body.profile.age,
        sex=body.profile.sex,
        budget_uah=body.budget_uah,
        workouts_per_week=body.workouts_per_week,
        fridge_items=body.fridge_items,
        note=body.note,
        previous_plan=body.previous_plan,
    )
    return await _run(body.silpo_access_token, prompt, apply=body.apply)


@app.post("/review", dependencies=[Depends(require_service_token)], response_model=AgentResponse)
async def review(body: ReviewRequest) -> AgentResponse:
    prompt = build_review_prompt(body.previous_plan)
    return await _run(body.silpo_access_token, prompt, apply=False)


async def _run(access_token: str, prompt: str, *, apply: bool) -> AgentResponse:
    try:
        async with SilpoMCP(settings.mcp_url, access_token) as mcp:
            agent = SilpoFitAgent(settings, mcp)
            await agent.prepare()
            result: AgentResult = await agent.run(prompt, apply=apply)
    except SilpoTokenExpired as exc:
        raise HTTPException(409, {"code": "silpo_token_expired", "message": str(exc)}) from exc
    except Exception as exc:
        raise HTTPException(502, f"agent failed: {exc}") from exc
    return AgentResponse(answer=result.answer, plan_to_persist=result.plan_to_persist)
