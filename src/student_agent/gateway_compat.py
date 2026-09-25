"""mcp v2 compatible gateway built on the starter's EvidenceGateway.

The starter module mcp_gateway.py stays exactly as released. It reads the mcp v1 attribute
names `isError` / `structuredContent`, while the installed mcp v2 exposes `is_error` /
`structured_content`. CompatGateway overrides `call` to accept both spellings; everything else
(session setup, envelope validation) is the same as the starter.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts
from .mcp_gateway import EvidenceGateway


def _first(obj: Any, *names: str) -> Any:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


class CompatGateway(EvidenceGateway):
    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        result = await self._session.call_tool(tool_name, arguments=payload)
        if _first(result, "is_error", "isError"):
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = _first(result, "structured_content", "structuredContent")
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence


@asynccontextmanager
async def connect_compat_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[CompatGateway]:
    """Same session setup as the starter's connect_gateway, yielding a CompatGateway."""
    headers = {"Authorization": f"Bearer {team_api_key}"}
    # Calls answer in about a second; 60 s (not the starter's 300 s) bounds a hung request.
    timeout = httpx2.Timeout(60.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield CompatGateway(session, contracts)