from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
from jsonschema import validate
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


class EvidenceGateway:
    def __init__(
        self,
        session: ClientSession,
        contracts: Contracts,
        evidence_log: Path | None = None,
        case_cache: dict[tuple, dict] | None = None,
    ) -> None:
        self._session = session
        self._contracts = contracts
        self._tools: dict[str, dict] | None = None
        # Supplied only by one run_case invocation, across its reconnect attempts.
        self._cache: dict[tuple, dict] = case_cache if case_cache is not None else {}
        self._evidence_log = evidence_log

    async def list_tools(self) -> list[str]:
        response = await self._session.list_tools()
        self._tools = {
            tool.name: getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", {})
            for tool in response.tools
        }
        return sorted(self._tools)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        if self._tools is None:
            await self.list_tools()
        if tool_name not in self._tools:
            raise RuntimeError(f"MCP tool {tool_name} was not discovered")
        validate(payload, self._tools[tool_name])
        key = (case_id, tool_name, tuple(sorted(arguments.items())))
        if key in self._cache:
            return self._cache[key]
        for attempt in range(3):
            try:
                result = await self._session.call_tool(tool_name, arguments=payload)
                break
            except (httpx2.TransportError, TimeoutError) as exc:
                if attempt == 2:
                    self._record(case_id, tool_name, arguments, {"error": type(exc).__name__})
                    raise RuntimeError(f"MCP transport failed for {tool_name}") from exc
                await asyncio.sleep(0.5 * 2**attempt)
        is_error = getattr(result, "is_error", getattr(result, "isError", False))
        if is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            self._record(case_id, tool_name, arguments, {"error": "tool_execution_error"})
            raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        self._cache[key] = evidence
        self._record(case_id, tool_name, arguments, evidence)
        return evidence

    def _record(self, case_id: str, tool: str, arguments: dict, response: dict) -> None:
        if self._evidence_log is not None:
            self._evidence_log.parent.mkdir(parents=True, exist_ok=True)
            with self._evidence_log.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "case_id": case_id,
                            "tool": tool,
                            "arguments": arguments,
                            "response": response,
                        }
                    )
                    + "\n"
                )


@asynccontextmanager
async def connect_gateway(
    endpoint: str,
    team_api_key: str,
    contracts: Contracts,
    evidence_log: Path | None = None,
    case_cache: dict[tuple, dict] | None = None,
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts, evidence_log, case_cache)
