"""The SilpoFit agent loop: Gemini decides, MCP and local tools act.

The agent holds no state across runs. Everything it needs — profile, goal,
budget, previous plan — arrives folded into the prompt text; everything it
produces comes back in the return value for the caller to persist.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable

from google import genai
from google.genai._gaos.lib.compat_errors import RateLimitError

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
        self._genai = genai.Client(api_key=settings.api_key)
        self._tools: list[dict[str, Any]] = []
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
        self._tools = declarations + local_tools.DECLARATIONS + [finalize.DECLARATION]
        print(f"[info] {len(self._tools)} tools available ({len(declarations)} from Silpo MCP)")

    async def run(self, user_input: str, *, apply: bool = False) -> AgentResult:
        self._apply = apply
        self._captured_plan = None

        system_instruction = prompts.SYSTEM_INSTRUCTION
        if not apply:
            system_instruction += prompts.DRY_RUN_NOTICE

        interaction = await self._create_interaction(
            input=user_input,
            system_instruction=system_instruction,
        )

        for step_number in range(self._settings.max_steps):
            calls = [s for s in interaction.steps if getattr(s, "type", None) == "function_call"]
            if not calls:
                return AgentResult(
                    answer=interaction.output_text or "(no answer)",
                    plan_to_persist=self._captured_plan,
                )

            results = []
            for call in calls:
                arguments = _as_dict(call.arguments)
                print(f"  [{step_number + 1}] {call.name}({_preview(arguments)})")
                text, is_error = await self._dispatch(call.name, arguments)
                if is_error:
                    print(f"      ! {text[:160]}")
                results.append(
                    {
                        "type": "function_result",
                        "name": call.name,
                        "call_id": call.id,
                        "is_error": is_error,
                        "result": [{"type": "text", "text": text}],
                    }
                )

            interaction = await self._create_interaction(
                input=results,
                system_instruction=system_instruction,
                previous_interaction_id=interaction.id,
            )

        raise RuntimeError(f"agent exceeded {self._settings.max_steps} tool steps")

    async def _create_interaction(self, **kwargs: Any) -> Any:
        """Wraps interactions.create with backoff on the free-tier rate limit."""
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                return await self._genai.aio.interactions.create(
                    model=self._settings.model,
                    tools=self._tools,
                    **kwargs,
                )
            except RateLimitError as exc:
                if attempt == MAX_RATE_LIMIT_RETRIES:
                    raise
                delay = _retry_after_seconds(exc) or 2**attempt
                print(f"      ! rate limited, retrying in {delay:.1f}s ({attempt + 1}/{MAX_RATE_LIMIT_RETRIES})")
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

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


def _retry_after_seconds(exc: RateLimitError) -> float | None:
    header = exc.response.headers.get("retry-after")
    if header is None:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def _as_dict(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return {}
    return {}


def _preview(arguments: dict[str, Any], limit: int = 110) -> str:
    text = json.dumps(arguments, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"
