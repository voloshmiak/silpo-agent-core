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
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Callable

from google import genai
from google.genai import errors, types

from . import finalize, local_tools, prompts
from .config import Settings
from .mcp_client import SilpoMCP
from .tool_bridge import MUTATING_TOOLS, select_tools

DRY_RUN_REFUSAL = (
    "Cart writes are disabled in review mode. The cart was NOT changed. "
    "Do not retry this tool; put the intended products in the plan's cart instead."
)

MAX_RATE_LIMIT_RETRIES = 5


@dataclass
class AgentResult:
    plan: dict[str, Any]


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
            print(f"[warn] Silpo MCP is missing expected tools: {', '.join(missing)}")

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
        print(f"[info] {len(all_declarations)} tools available ({len(declarations)} from Silpo MCP)")

    async def run(self, user_input: str, *, apply: bool = False) -> AgentResult:
        """Drains run_stream and returns just the finished plan."""
        async for event in self.run_stream(user_input, apply=apply):
            if event["type"] == "plan":
                return AgentResult(plan=event["plan"])
            if event["type"] == "error":
                raise RuntimeError(event["message"])
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

        for step_number in range(self._settings.max_steps):
            parts = await self._generate(history, system_instruction)
            history.append(types.Content(role="model", parts=parts or [types.Part(text="")]))

            calls = [part.function_call for part in parts if part.function_call]
            if not calls:
                yield _event(
                    "error",
                    message=f"agent stopped without calling {finalize.TOOL_NAME}",
                    text=_truncate(_text_of(parts), 500),
                )
                return

            response_parts = []
            for call in calls:
                arguments = dict(call.args or {})
                print(f"  [{step_number + 1}] {call.name}({_preview(arguments)})")
                yield _event("tool_call", tool=call.name, args=arguments)

                result_text, is_error = await self._dispatch(call.name, arguments)
                if is_error:
                    print(f"      ! {result_text[:160]}")
                yield _event("tool_result", tool=call.name, ok=not is_error, result=_truncate(result_text))

                response_parts.append(
                    types.Part.from_function_response(
                        name=call.name,
                        response={"error": result_text} if is_error else {"result": result_text},
                    )
                )

            if self._captured_plan is not None:
                yield _event("plan", plan=self._captured_plan)
                return

            history.append(types.Content(role="user", parts=response_parts))

        yield _event("error", message=f"agent exceeded {self._settings.max_steps} tool steps")

    async def _generate(self, history: list[types.Content], system_instruction: str) -> list[types.Part]:
        """Runs one model turn, retrying while the API rate-limits us.

        Returns the raw `Part` objects as the API sent them (not rebuilt from
        `FunctionCall`/text alone) — Gemini 3 attaches a `thought_signature`
        to function-call parts that must round-trip back unchanged on the next
        turn, or the API rejects the request.
        """
        response = None
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                response = await self._genai.aio.models.generate_content(
                    model=self._settings.model,
                    contents=history,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        tools=self._tools,
                    ),
                )
                break
            except errors.ClientError as exc:
                if exc.code != 429 or attempt == MAX_RATE_LIMIT_RETRIES:
                    raise
                delay = _retry_after_seconds(exc) or 2**attempt
                print(f"      ! rate limited, retrying in {delay:.1f}s ({attempt + 1}/{MAX_RATE_LIMIT_RETRIES})")
                await asyncio.sleep(delay)
        assert response is not None

        content = response.candidates[0].content if response.candidates else None
        return list((content.parts if content else None) or [])

    async def _dispatch(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Runs one tool call. Tool failures come back as text, never as exceptions."""
        if name in MUTATING_TOOLS and not self._apply:
            return DRY_RUN_REFUSAL, True
        try:
            if name in self._mcp_tool_names:
                return await self._mcp.call_tool(name, arguments), False
            handler = self._local.get(name)
            if handler is None:
                return f"Unknown tool: {name}", True
            result = handler(**arguments)
            return json.dumps(result, ensure_ascii=False, default=str), False
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}", True

    def _finalize(self, **arguments: Any) -> str:
        """Validates the plan before capturing it.

        A rejection travels back to the model as a tool error, so a malformed
        call costs one retry instead of failing the whole run.
        """
        self._captured_plan = finalize.validate(arguments)
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


def _preview(arguments: dict[str, Any], limit: int = 110) -> str:
    text = json.dumps(arguments, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _truncate(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[:limit] + "…"
