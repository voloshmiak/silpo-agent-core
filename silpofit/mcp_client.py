"""Local MCP client for the official Silpo server.

The Interactions API can attach a remote MCP server itself, but that path does
not work with Gemini 3 models yet, so the agent runs its own MCP client and
bridges the server's tools into ordinary function calling.

The agent never performs the Silpo OAuth flow itself — the calling backend
owns user authorization and hands this class an already-valid access token
for a single request's lifetime.
"""

import json
import logging
import time
from contextlib import AsyncExitStack
from typing import Any

from mcp import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.types import CallToolResult, Tool

from .logs import preview

log = logging.getLogger(__name__)


class SilpoTokenExpired(RuntimeError):
    """The Silpo access token the caller supplied was rejected (HTTP 401)."""


class SilpoMCP:
    """A Silpo MCP session authenticated with a caller-supplied access token."""

    def __init__(self, mcp_url: str, access_token: str) -> None:
        self._mcp_url = mcp_url
        self._access_token = access_token
        self._stack = AsyncExitStack()
        self._client: Client | None = None

    async def __aenter__(self) -> "SilpoMCP":
        http_client = await self._stack.enter_async_context(
            create_mcp_http_client(headers={"Authorization": f"Bearer {self._access_token}"})
        )
        # The MCP SDK swallows the real HTTP status of a rejected token into a
        # generic JSON-RPC "-32603 internal error", so check it ourselves first
        # to give the caller an unambiguous, machine-readable failure.
        await self._check_token(http_client)
        self._client = await self._stack.enter_async_context(
            Client(streamable_http_client(self._mcp_url, http_client=http_client))
        )
        return self

    async def _check_token(self, http_client) -> None:
        response = await http_client.post(
            self._mcp_url,
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "silpofit-agent", "version": "0.1"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
        if response.status_code == 401:
            log.warning("Silpo rejected the access token (401) at %s", self._mcp_url)
            raise SilpoTokenExpired("Silpo rejected the supplied access token (401)")
        if response.status_code >= 400:
            log.error(
                "Silpo MCP initialize failed: HTTP %d — %s",
                response.status_code,
                preview(response.text, 400),
            )
        response.raise_for_status()
        log.info("Silpo MCP session opened at %s", self._mcp_url)

    async def __aexit__(self, *exc_info) -> None:
        await self._stack.aclose()
        self._client = None

    @property
    def client(self) -> Client:
        if self._client is None:
            raise RuntimeError("SilpoMCP used outside of its async context")
        return self._client

    async def list_tools(self) -> list[Tool]:
        tools = (await self.client.list_tools()).tools
        log.info("Silpo MCP exposes %d tools", len(tools))
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Calls an MCP tool and flattens the result into text for the model."""
        started = time.monotonic()
        try:
            result = await self.client.call_tool(name, arguments)
        except Exception:
            # The agent turns this into a tool error for the model; without a
            # log line here the transport failure itself would be invisible.
            log.exception("MCP %s raised after %.0fms", name, (time.monotonic() - started) * 1000)
            raise
        text = _flatten(result)
        if result.is_error:
            log.warning("MCP %s returned an error result: %s", name, preview(text, 600))
        log.debug("MCP %s: %.0fms, %d chars", name, (time.monotonic() - started) * 1000, len(text))
        return text


def _flatten(result: CallToolResult) -> str:
    if result.structured_content is not None:
        return json.dumps(result.structured_content, ensure_ascii=False)
    parts: list[str] = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else block.model_dump_json())
    return "\n".join(parts) if parts else "(empty result)"
