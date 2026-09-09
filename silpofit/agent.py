"""The SilpoFit agent loop: Gemini decides, MCP and local tools act.

The agent holds no state across runs. Everything it needs — profile, goal,
budget, previous plan — arrives folded into the prompt text; everything it
produces comes back in the return value for the caller to persist.

`run_stream` is the source of truth for the loop; `run` just drains it. Each
step yields `tool_call`/`tool_result` events while the model works through
the pipeline, and the run ends the moment finalize_plan lands: that call
carries the whole structured plan, so there is nothing left to say in prose
and no reason to spend another model turn saying it. A turn that comes back
without tool calls means the model stopped short of finalize_plan — that is
a failed run, not an answer.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable

from google import genai
from google.genai import errors, types

from . import finalize, local_tools, prompts
from .config import Settings
from .logs import preview
from .mcp_client import SilpoMCP
from .tool_bridge import MUTATING_TOOLS, select_tools

log = logging.getLogger(__name__)

DRY_RUN_REFUSAL = (
    "Cart writes are disabled in review mode. The cart was NOT changed. "
    "Do not retry this tool; put the intended products in the plan's cart instead."
)

MAX_RATE_LIMIT_RETRIES = 5


@dataclass
class AgentResult:
    plan: dict[str, Any]


@dataclass
class _ModelTurn:
    """One model turn plus the metadata that explains an empty one.

    A run that ends without finalize_plan almost always ends because the model
    turn came back with no function calls, and `finish_reason` is what says
    why (MAX_TOKENS, SAFETY, a plain text answer…). Keeping it next to the
    parts means the failure event can carry the reason instead of just the
    symptom.
    """

    parts: list[types.Part] = field(default_factory=list)
    finish_reason: str = "UNKNOWN"
    usage: dict[str, int] = field(default_factory=dict)


class SilpoFitAgent:
    def __init__(self, settings: Settings, mcp: SilpoMCP) -> None:
        self._settings = settings
        self._mcp = mcp
        self._genai = genai.Client(api_key=settings.api_key, vertexai=True)
        self._tools: list[types.Tool] = []
        self._local: dict[str, Callable[..., Any]] = {}
        self._mcp_tool_names: set[str] = set()
        self._apply = False
        self._captured_plan: dict[str, Any] | None = None

    async def prepare(self) -> None:
        """Loads the MCP tool list and builds the full tool set for the model."""
        mcp_tools = await self._mcp.list_tools()
        declarations, missing = select_tools(mcp_tools)
        if missing:
            log.warning("Silpo MCP is missing expected tools: %s", ", ".join(missing))

        self._mcp_tool_names = {d["name"] for d in declarations}
        self._local = {**local_tools.HANDLERS, finalize.TOOL_NAME: self._finalize}
        all_declarations = declarations + local_tools.DECLARATIONS + [finalize.DECLARATION]
        self._tools = [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name=d["name"],
                        description=d["description"],
                        parameters_json_schema=d["parameters"],
                    )
                    for d in all_declarations
                ]
            )
        ]
        log.info(
            "tools ready: %d total (%d Silpo MCP, %d local, 1 finalize)",
            len(all_declarations),
            len(declarations),
            len(local_tools.DECLARATIONS),
        )

    async def run(self, user_input: str, *, apply: bool = False) -> AgentResult:
        """Drains run_stream and returns just the finished plan."""
        async for event in self.run_stream(user_input, apply=apply):
            if event["type"] == "plan":
                return AgentResult(plan=event["plan"])
            if event["type"] == "error":
                raise RuntimeError(event["message"])
        log.error("run_stream ended without a terminal event — this should be unreachable")
        raise RuntimeError("agent produced no plan")

    async def run_stream(self, user_input: str, *, apply: bool = False) -> AsyncGenerator[dict[str, Any], None]:
        """Runs the agent loop, yielding one progress event per step.

        Event types: `tool_call`, `tool_result` (progress), `plan`
        (terminal, success — carries the structured plan), `error`
        (terminal, failure).
        """
        self._apply = apply
        self._captured_plan = None

        system_instruction = prompts.SYSTEM_INSTRUCTION
        if not apply:
            system_instruction += prompts.DRY_RUN_NOTICE

        history: list[types.Content] = [types.Content(role="user", parts=[types.Part(text=user_input)])]

        started = time.monotonic()
        calls_made = 0
        calls_failed = 0
        log.info(
            "run start: model=%s apply=%s max_steps=%d prompt=%d chars",
            self._settings.model,
            apply,
            self._settings.max_steps,
            len(user_input),
        )
        log.debug("prompt: %s", preview(user_input, 2000))

        for step_number in range(1, self._settings.max_steps + 1):
            turn = await self._generate(history, system_instruction, step_number)
            history.append(types.Content(role="model", parts=turn.parts or [types.Part(text="")]))

            calls = [part.function_call for part in turn.parts if part.function_call]
            if not calls:
                # The single most common way a run dies. The model's own words
                # and the finish reason are the whole diagnosis, so log them in
                # full and hand a trimmed copy to the caller.
                text = _text_of(turn.parts)
                log.error(
                    "run failed at step %d/%d: no tool calls, finish_reason=%s, parts=%d, "
                    "calls_so_far=%d, model said: %s",
                    step_number,
                    self._settings.max_steps,
                    turn.finish_reason,
                    len(turn.parts),
                    calls_made,
                    preview(text, 2000) or "(nothing)",
                )
                yield _event(
                    "error",
                    message=f"agent stopped without calling {finalize.TOOL_NAME}",
                    finish_reason=turn.finish_reason,
                    step=step_number,
                    text=_truncate(text, 500),
                )
                return

            response_parts = []
            for call in calls:
                arguments = dict(call.args or {})
                calls_made += 1
                log.info("step %d -> %s(%s)", step_number, call.name, preview(arguments, 400))
                yield _event("tool_call", tool=call.name, args=arguments)

                call_started = time.monotonic()
                result_text, is_error = await self._dispatch(call.name, arguments)
                elapsed_ms = (time.monotonic() - call_started) * 1000

                if is_error:
                    calls_failed += 1
                    log.warning(
                        "step %d <- %s failed in %.0fms: %s",
                        step_number,
                        call.name,
                        elapsed_ms,
                        preview(result_text, 600),
                    )
                else:
                    log.info(
                        "step %d <- %s ok in %.0fms (%d chars)",
                        step_number,
                        call.name,
                        elapsed_ms,
                        len(result_text),
                    )
                    log.debug("step %d <- %s result: %s", step_number, call.name, preview(result_text, 1000))

                yield _event("tool_result", tool=call.name, ok=not is_error, result=_truncate(result_text))

                response_parts.append(
                    types.Part.from_function_response(
                        name=call.name,
                        response={"error": result_text} if is_error else {"result": result_text},
                    )
                )

            if self._captured_plan is not None:
                log.info(
                    "run ok in %.1fs: %d steps, %d tool calls (%d failed)",
                    time.monotonic() - started,
                    step_number,
                    calls_made,
                    calls_failed,
                )
                yield _event("plan", plan=self._captured_plan)
                return

            history.append(types.Content(role="user", parts=response_parts))

        log.error(
            "run failed: hit max_steps=%d after %.1fs, %d tool calls (%d failed), no %s",
            self._settings.max_steps,
            time.monotonic() - started,
            calls_made,
            calls_failed,
            finalize.TOOL_NAME,
        )
        yield _event(
            "error",
            message=f"agent exceeded {self._settings.max_steps} tool steps",
            tool_calls=calls_made,
        )

    async def _generate(
        self, history: list[types.Content], system_instruction: str, step_number: int
    ) -> _ModelTurn:
        """Runs one model turn, retrying while the API rate-limits us.

        Returns the raw `Part` objects as the API sent them (not rebuilt from
        `FunctionCall`/text alone) — Gemini 3 attaches a `thought_signature`
        to function-call parts that must round-trip back unchanged on the next
        turn, or the API rejects the request.
        """
        response = None
        started = time.monotonic()
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                response = await self._genai.aio.models.generate_content(
                    model=self._settings.model,
                    contents=history,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        tools=self._tools,
                        thinking_config=types.ThinkingConfig(
                            thinking_level=self._settings.thinking_level
                        ),
                    ),
                )
                break
            except errors.ClientError as exc:
                if exc.code != 429 or attempt == MAX_RATE_LIMIT_RETRIES:
                    log.error(
                        "model call failed at step %d (history=%d turns, code=%s): %s",
                        step_number,
                        len(history),
                        exc.code,
                        preview(str(exc), 800),
                    )
                    raise
                delay = _retry_after_seconds(exc) or 2**attempt
                log.warning(
                    "rate limited at step %d, retrying in %.1fs (%d/%d)",
                    step_number,
                    delay,
                    attempt + 1,
                    MAX_RATE_LIMIT_RETRIES,
                )
                await asyncio.sleep(delay)
            except Exception:
                log.exception("model call raised at step %d (history=%d turns)", step_number, len(history))
                raise
        assert response is not None

        candidate = response.candidates[0] if response.candidates else None
        content = candidate.content if candidate else None
        turn = _ModelTurn(
            parts=list((content.parts if content else None) or []),
            finish_reason=_finish_reason(candidate),
            usage=_usage(response),
        )
        log.info(
            "step %d model turn: %.1fs finish=%s parts=%d tokens=%s",
            step_number,
            time.monotonic() - started,
            turn.finish_reason,
            len(turn.parts),
            preview(turn.usage, 120) if turn.usage else "?",
        )
        # Two silent killers: a blocked prompt and a truncated turn both come
        # back as an ordinary response with nothing useful inside it.
        feedback = getattr(response, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            log.error("step %d prompt blocked by the API: %s", step_number, feedback.block_reason)
        if candidate is not None and getattr(candidate, "finish_message", None):
            log.warning("step %d finish_message: %s", step_number, preview(candidate.finish_message, 500))
        return turn

    async def _dispatch(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Runs one tool call. Tool failures come back as text, never as exceptions."""
        if name in MUTATING_TOOLS and not self._apply:
            log.info("tool %s refused: cart writes are off (apply=False)", name)
            return DRY_RUN_REFUSAL, True
        try:
            if name in self._mcp_tool_names:
                return await self._mcp.call_tool(name, arguments), False
            handler = self._local.get(name)
            if handler is None:
                log.error("model called an unknown tool: %s", name)
                return f"Unknown tool: {name}", True
            result = handler(**arguments)
            return json.dumps(result, ensure_ascii=False, default=str), False
        except Exception as exc:
            # A ValueError is a tool rejecting its arguments — expected, and
            # already spelled out in the message the model gets back. Anything
            # else is a real crash and is worth a traceback.
            if not isinstance(exc, ValueError):
                log.exception("tool %s raised %s, args=%s", name, type(exc).__name__, preview(arguments, 400))
            return f"{type(exc).__name__}: {exc}", True

    def _finalize(self, **arguments: Any) -> str:
        """Validates the plan before capturing it.

        A rejection travels back to the model as a tool error, so a malformed
        call costs one retry instead of failing the whole run.
        """
        try:
            plan = finalize.validate(arguments)
        except ValueError as exc:
            log.warning("%s rejected: %s", finalize.TOOL_NAME, preview(str(exc), 1500))
            log.debug("%s payload was: %s", finalize.TOOL_NAME, preview(arguments, 4000))
            raise
        self._captured_plan = plan
        log.info("%s accepted: %s", finalize.TOOL_NAME, _plan_shape(plan))
        return "recorded"


def _text_of(parts: list[types.Part]) -> str:
    return "".join(part.text for part in parts if part.text and not part.thought)


def _event(type_: str, **data: Any) -> dict[str, Any]:
    return {"type": type_, **data}


def _retry_after_seconds(exc: errors.ClientError) -> float | None:
    response = exc.response
    if response is None:
        return None
    header = response.headers.get("retry-after")
    if header is None:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def _finish_reason(candidate: Any) -> str:
    """The candidate's finish reason as a plain, loggable name."""
    if candidate is None:
        return "NO_CANDIDATES"
    reason = getattr(candidate, "finish_reason", None)
    if reason is None:
        return "NONE"
    return getattr(reason, "name", str(reason))


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return {}
    fields = (
        ("prompt", "prompt_token_count"),
        ("output", "candidates_token_count"),
        ("thoughts", "thoughts_token_count"),
        ("total", "total_token_count"),
    )
    return {label: value for label, attr in fields if (value := getattr(usage, attr, None)) is not None}


def _plan_shape(plan: dict[str, Any]) -> str:
    """A one-line fingerprint of a finished plan, for the run's closing log."""
    summary = plan.get("summary") or {}
    return (
        f"{len(plan.get('days') or [])} days, {len(plan.get('cart') or [])} cart items, "
        f"{summary.get('total_uah', '?')}/{summary.get('budget_uah', '?')} UAH"
    )


def _truncate(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[:limit] + "…"
