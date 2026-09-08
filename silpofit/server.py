"""HTTP entry point.

SilpoFit runs as a stateless agent service: the calling backend owns user
authorization, profiles and history, and sends everything the agent needs in
one request. The agent holds nothing between requests — no tokens, no plans.
"""

import json
import logging
import time
from typing import Any, AsyncGenerator, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import logs
from .agent import AgentResult, SilpoFitAgent
from .config import Settings
from .mcp_client import SilpoMCP, SilpoTokenExpired
from .plan_schema import Plan
from .prompts import build_plan_prompt

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


log.info(
    "SilpoFit starting: model=%s max_steps=%d mcp=%s service_tokens=%d",
    settings.model,
    settings.max_steps,
    settings.mcp_url,
    len(settings.service_tokens),
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/plan", dependencies=[Depends(require_service_token)], response_model=Plan)
async def plan(body: PlanRequest) -> Plan:
    """The finished weekly plan: targets, seven days of meals, cart, summary."""
    return await _run(body.silpo_access_token, _plan_prompt(body), apply=body.apply)


@app.post("/plan/stream", dependencies=[Depends(require_service_token)])
async def plan_stream(body: PlanRequest) -> StreamingResponse:
    """SSE: `tool_call`/`tool_result` events while the agent is calling MCP
    tools, then one terminal `plan` event carrying the same JSON object that
    `POST /plan` returns (or `error` if the run fails). No prose is streamed —
    the plan is the answer."""
    return StreamingResponse(
        _sse(body.silpo_access_token, _plan_prompt(body), apply=body.apply),
        media_type="text/event-stream",
    )


def _plan_prompt(body: PlanRequest) -> str:
    return build_plan_prompt(
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


async def _run(access_token: str, prompt: str, *, apply: bool) -> Plan:
    run_id = logs.new_run_id()
    log.info("POST /plan (apply=%s, prompt=%d chars)", apply, len(prompt))
    started = time.monotonic()
    try:
        async with SilpoMCP(settings.mcp_url, access_token) as mcp:
            agent = SilpoFitAgent(settings, mcp)
            await agent.prepare()
            result: AgentResult = await agent.run(prompt, apply=apply)
    except SilpoTokenExpired as exc:
        log.warning("POST /plan → 409 silpo_token_expired after %.1fs", time.monotonic() - started)
        raise HTTPException(409, {"code": "silpo_token_expired", "message": str(exc)}) from exc
    except Exception as exc:
        log.exception("POST /plan → 502 after %.1fs (run_id=%s)", time.monotonic() - started, run_id)
        raise HTTPException(502, f"agent failed: {exc} (run_id={run_id})") from exc
    log.info("POST /plan → 200 in %.1fs", time.monotonic() - started)
    return Plan.model_validate(result.plan)


async def _sse(access_token: str, prompt: str, *, apply: bool) -> AsyncGenerator[str, None]:
    """Same run as `_run`, but yielded as SSE frames instead of collected into
    one response. The HTTP status and headers are already sent by the time
    an agent failure can happen, so failures become a terminal `error` event
    instead of an HTTP error status.

    Every frame carries the run id, and the stream is guaranteed to end on a
    `plan` or an `error` — a client that sees neither has lost the connection,
    which is the one failure this generator cannot report to it."""
    run_id = logs.new_run_id()
    log.info("POST /plan/stream (apply=%s, prompt=%d chars)", apply, len(prompt))
    started = time.monotonic()
    counts: dict[str, int] = {}
    terminal: str | None = None
    try:
        async with SilpoMCP(settings.mcp_url, access_token) as mcp:
            agent = SilpoFitAgent(settings, mcp)
            await agent.prepare()
            async for event in agent.run_stream(prompt, apply=apply):
                counts[event["type"]] = counts.get(event["type"], 0) + 1
                if event["type"] in ("plan", "error"):
                    terminal = event["type"]
                yield _frame(event, run_id)

        if terminal is None:
            # The agent loop always ends on a terminal event, so reaching this
            # means the loop itself was cut short — worth a loud line, and the
            # client still needs an ending it can act on.
            log.error("stream ended with no terminal event after %.1fs, events=%s", time.monotonic() - started, counts)
            yield _frame({"type": "error", "message": "agent stream ended without a plan"}, run_id)
            terminal = "error"
    except SilpoTokenExpired as exc:
        log.warning("stream → silpo_token_expired after %.1fs", time.monotonic() - started)
        terminal = "error"
        yield _frame({"type": "error", "code": "silpo_token_expired", "message": str(exc)}, run_id)
    except GeneratorExit:
        # The client hung up mid-run: nothing can be sent, so the only trace
        # left of this run is this line.
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
    """One SSE frame, stamped with the run id so a frontend error report can
    be matched to the run's log lines."""
    return f"data: {json.dumps({**event, 'run_id': run_id}, ensure_ascii=False)}\n\n"
