"""The SilpoFit agent loop: Gemini decides, MCP and local tools act.

The agent holds no state across runs. Everything it needs — profile, goal,
budget, previous plan — arrives folded into the prompt text; everything it
produces comes back in the return value for the caller to persist.

`run_stream` is the source of truth for the loop; `run` just drains it. Two
phases fall out of the same loop, not two separate code paths: while the
model is emitting tool calls, each step yields `tool_call`/`tool_result`
events (nothing to token-stream there — the model isn't writing text). Once
a turn comes back with no tool calls, that's the synthesis turn, and its
text streams out token by token, ending in one `plan` event with the
finalized plan and the full answer.
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
    "Do not retry this tool; list the intended products in your final answer instead."
)

MAX_RATE_LIMIT_RETRIES = 5


@dataclass
class AgentResult:
    answer: str
    plan_to_persist: dict[str, Any] | None


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
        """Drains run_stream and returns just the final answer + plan."""
        answer = "(no answer)"
        async for event in self.run_stream(user_input, apply=apply):
            if event["type"] == "plan":
                answer = event["answer"]
            elif event["type"] == "error":
                raise RuntimeError(event["message"])
        return AgentResult(answer=answer, plan_to_persist=self._captured_plan)

    async def run_stream(self, user_input: str, *, apply: bool = False) -> AsyncGenerator[dict[str, Any], None]:
        """Runs the agent loop, yielding one progress event per step.

        Event types: `tool_call`, `tool_result` (orchestration phase),
        `token` (synthesis phase), `plan` (terminal, success), `error`
        (terminal, failure).
        """
        self._apply = apply
        self._captured_plan = None

        system_instruction = prompts.SYSTEM_INSTRUCTION
        if not apply:
            system_instruction += prompts.DRY_RUN_NOTICE

        history: list[types.Content] = [types.Content(role="user", parts=[types.Part(text=user_input)])]

        for step_number in range(self._settings.max_steps):
            parts: list[types.Part] = []
            text = ""
            async for kind, payload in self._generate_streaming(history, system_instruction):
                if kind == "token":
                    yield _event("token", text=payload)
                else:
                    parts, text = payload

            history.append(types.Content(role="model", parts=parts or [types.Part(text="")]))

            calls = [part.function_call for part in parts if part.function_call]
            if not calls:
                yield _event("plan", answer=text or "(no answer)", plan=self._captured_plan)
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
            history.append(types.Content(role="user", parts=response_parts))

        yield _event("error", message=f"agent exceeded {self._settings.max_steps} tool steps")

    async def _generate_streaming(
        self, history: list[types.Content], system_instruction: str
    ) -> AsyncGenerator[tuple[str, Any], None]:
        """Streams one model turn.

        Yields `("token", text)` for every text chunk as it arrives, then a
        final `("done", (parts, full_text))` once the turn is complete.
        `parts` are the raw `Part` objects as the API sent them (not rebuilt
        from `FunctionCall`/text alone) — Gemini 3 attaches a
        `thought_signature` to function-call parts that must round-trip back
        unchanged on the next turn, or the API rejects the request. Function
        args on the tools this agent uses are small, so calls are treated as
        complete wherever they appear in the stream rather than reassembled
        from partial-arg deltas.
        """
        stream = None
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                stream = await self._genai.aio.models.generate_content_stream(
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
        assert stream is not None

        parts: list[types.Part] = []
        text_chunks: list[str] = []
        async for chunk in stream:
            content = chunk.candidates[0].content if chunk.candidates else None
            for part in (content.parts if content else None) or []:
                parts.append(part)
                if part.text and not part.thought:
                    text_chunks.append(part.text)
                    yield "token", part.text
        yield "done", (parts, "".join(text_chunks))

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
        self._captured_plan = arguments
        return finalize.ack(arguments)


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
