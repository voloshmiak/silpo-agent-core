import json
import logging
import time
from typing import Any, AsyncGenerator

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import logs
from .agent import AgentResult, SilpoFitAgent
from .config import Settings
from .mcp_client import SilpoMCP, SilpoTokenExpired
from .plan_schema import Plan
from .prompts import build_plan_prompt
from .request_schema import PlanRequest
from .validator import PlanContext

logs.setup_logging()
log = logging.getLogger(__name__)

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
    if not settings.service_tokens:
        raise HTTPException(500, "SILPOFIT_SERVICE_TOKENS is not configured")
    if creds is None or creds.credentials not in settings.service_tokens:
        raise HTTPException(401, "invalid or missing service token")


log.info(
    "SilpoFit starting: model=%s thinking=%s max_steps=%d validation=%s mcp=%s service_tokens=%d",
    settings.model,
    settings.thinking_level,
    settings.max_steps,
    f"{settings.review_model}/{settings.review_thinking_level} x{settings.validation_rounds}"
    if settings.validation_rounds > 0
    else "off",
    settings.mcp_url,
    len(settings.service_tokens),
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/plan", dependencies=[Depends(require_service_token)], response_model=Plan)
async def plan(body: PlanRequest) -> Plan:
    """The finished weekly plan: targets, seven days of meals, cart, summary."""
    return await _run(
        body.silpo_access_token,
        _plan_prompt(body),
        apply=body.apply,
        context=PlanContext.from_request(body),
    )


@app.post("/plan/stream", dependencies=[Depends(require_service_token)])
async def plan_stream(body: PlanRequest) -> StreamingResponse:
    """SSE: `tool_call`/`tool_result` events while the agent is calling MCP
    tools, a `validation` event every time the plan is reviewed, then one
    terminal `plan` event carrying the same JSON object that `POST /plan`
    returns (or `error` if the run fails). No prose is streamed — the plan is
    the answer."""
    return StreamingResponse(
        _sse(
            body.silpo_access_token,
            _plan_prompt(body),
            apply=body.apply,
            context=PlanContext.from_request(body),
        ),
        media_type="text/event-stream",
    )


def _plan_prompt(body: PlanRequest) -> str:
    return build_plan_prompt(
        weight_kg=body.profile.weight_kg,
        target_weight_kg=body.profile.target_weight_kg,
        height_cm=body.profile.height_cm,
        age=body.profile.age,
        sex=body.profile.sex,
        goal=body.goal,
        weekly_pace_kg=body.weekly_pace_kg,
        budget_uah=body.budget_uah,
        promo_priority=body.promo_priority,
        delivery_included=body.delivery_included,
        workouts_per_week=body.workouts_per_week,
        workout_schedule=body.workout_schedule,
        diet_type=body.diet_type,
        allergens=body.allergens,
        excluded_products=body.excluded_products,
        fridge_items=body.fridge_items,
        note=body.note,
        previous_plan=body.previous_plan,
    )


async def _run(access_token: str, prompt: str, *, apply: bool, context: PlanContext) -> Plan:
    run_id = logs.new_run_id()
    log.info("POST /plan (apply=%s, prompt=%d chars)", apply, len(prompt))
    started = time.monotonic()
    try:
        async with SilpoMCP(settings.mcp_url, access_token) as mcp:
            agent = SilpoFitAgent(settings, mcp)
            await agent.prepare()
            result: AgentResult = await agent.run(prompt, apply=apply, context=context)
    except SilpoTokenExpired as exc:
        log.warning("POST /plan → 409 silpo_token_expired after %.1fs", time.monotonic() - started)
        raise HTTPException(409, {"code": "silpo_token_expired", "message": str(exc)}) from exc
    except Exception as exc:
        log.exception("POST /plan → 502 after %.1fs (run_id=%s)", time.monotonic() - started, run_id)
        raise HTTPException(502, f"agent failed: {exc} (run_id={run_id})") from exc
    log.info("POST /plan → 200 in %.1fs", time.monotonic() - started)
    return Plan.model_validate(result.plan)


async def _sse(
    access_token: str, prompt: str, *, apply: bool, context: PlanContext
) -> AsyncGenerator[str, None]:
    run_id = logs.new_run_id()
    log.info("POST /plan/stream (apply=%s, prompt=%d chars)", apply, len(prompt))
    started = time.monotonic()
    counts: dict[str, int] = {}
    terminal: str | None = None
    try:
        async with SilpoMCP(settings.mcp_url, access_token) as mcp:
            agent = SilpoFitAgent(settings, mcp)
            await agent.prepare()
            async for event in agent.run_stream(prompt, apply=apply, context=context):
                counts[event["type"]] = counts.get(event["type"], 0) + 1
                if event["type"] in ("plan", "error"):
                    terminal = event["type"]
                yield _frame(event, run_id)

        if terminal is None:
            log.error("stream ended with no terminal event after %.1fs, events=%s", time.monotonic() - started, counts)
            yield _frame({"type": "error", "message": "agent stream ended without a plan"}, run_id)
            terminal = "error"
    except SilpoTokenExpired as exc:
        log.warning("stream → silpo_token_expired after %.1fs", time.monotonic() - started)
        terminal = "error"
        yield _frame({"type": "error", "code": "silpo_token_expired", "message": str(exc)}, run_id)
    except GeneratorExit:
        log.warning(
            "client disconnected after %.1fs, events=%s, terminal=%s",
            time.monotonic() - started,
            counts,
            terminal or "none",
        )
        raise
    except Exception as exc:
        log.exception("stream failed after %.1fs, events=%s", time.monotonic() - started, counts)
        terminal = "error"
        yield _frame({"type": "error", "message": f"agent failed: {exc}"}, run_id)
    finally:
        log.info(
            "POST /plan/stream done in %.1fs: terminal=%s, events=%s",
            time.monotonic() - started,
            terminal or "none",
            counts,
        )


def _frame(event: dict[str, Any], run_id: str) -> str:
    return f"data: {json.dumps({**event, 'run_id': run_id}, ensure_ascii=False)}\n\n"
