"""Local MCP client for the official Silpo server.

The Interactions API can attach a remote MCP server itself, but that path does
not work with Gemini 3 models yet, so the agent runs its own MCP client and
bridges the server's tools into ordinary function calling.

The agent never performs the Silpo OAuth flow itself — the calling backend
owns user authorization and hands this class an already-valid access token
for a single request's lifetime.
"""

import json
from contextlib import AsyncExitStack
from typing import Any

from mcp import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.types import CallToolResult, Tool


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
            raise SilpoTokenExpired("Silpo rejected the supplied access token (401)")
        response.raise_for_status()

    async def __aexit__(self, *exc_info) -> None:
        await self._stack.aclose()
        self._client = None

    @property
    def client(self) -> Client:
        if self._client is None:
            raise RuntimeError("SilpoMCP used outside of its async context")
        return self._client

    async def list_tools(self) -> list[Tool]:
        return (await self.client.list_tools()).tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Calls an MCP tool and flattens the result into text for the model."""
        result = await self.client.call_tool(name, arguments)
        return _flatten(result)


def _flatten(result: CallToolResult) -> str:
    if result.structured_content is not None:
        return json.dumps(result.structured_content, ensure_ascii=False)
    parts: list[str] = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else block.model_dump_json())
    return "\n".join(parts) if parts else "(empty result)"
