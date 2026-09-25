from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway
from student_agent.submission import validate_case_consistency
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


class Session:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="get_order",
                    input_schema={
                        "type": "object",
                        "required": ["case_id", "order_id"],
                        "properties": {
                            "case_id": {"type": "string"},
                            "order_id": {"type": "string"},
                        },
                    },
                )
            ]
        )

    async def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        return SimpleNamespace(
            is_error=False,
            structured_content={
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": f"ev_test_only_{len(self.calls):020d}",
                "result_hash": "sha256:" + "0" * 64,
                "domain": "order",
                "data": {"order_id": arguments["order_id"]},
            },
        )


def contracts():
    return Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")


def test_gateway_cache_is_case_scoped(tmp_path):
    async def run():
        session = Session()
        gateway = EvidenceGateway(session, contracts(), tmp_path / "evidence.jsonl")
        first = await gateway.call("get_order", case_id="CASE_ONE", order_id="order-1")
        again = await gateway.call("get_order", case_id="CASE_ONE", order_id="order-1")
        other = await gateway.call("get_order", case_id="CASE_TWO", order_id="order-1")
        assert first == again
        assert first["evidence_ref"] != other["evidence_ref"]
        assert len(session.calls) == 2
        assert len((tmp_path / "evidence.jsonl").read_text().splitlines()) == 2

    asyncio.run(run())


def test_gateway_rejects_undiscovered_tools_and_bad_arguments():
    async def run():
        session = Session()
        gateway = EvidenceGateway(session, contracts())
        with pytest.raises(RuntimeError, match="not discovered"):
            await gateway.call("imaginary_tool", case_id="CASE_ONE")
        with pytest.raises(ValidationError):
            await gateway.call("get_order", case_id="CASE_ONE")
        assert session.calls == []

    asyncio.run(run())


def test_successful_evidence_survives_reconnect_but_not_a_new_run():
    async def run():
        cache = {}
        first_session, reconnected_session, new_run_session = Session(), Session(), Session()
        first = EvidenceGateway(first_session, contracts(), case_cache=cache)
        evidence = await first.call("get_order", case_id="CASE_ONE", order_id="order-1")
        reconnected = EvidenceGateway(reconnected_session, contracts(), case_cache=cache)
        assert (
            await reconnected.call("get_order", case_id="CASE_ONE", order_id="order-1") == evidence
        )
        assert reconnected_session.calls == []
        await reconnected.call("get_order", case_id="CASE_TWO", order_id="order-1")
        assert len(reconnected_session.calls) == 1
        new_run = EvidenceGateway(new_run_session, contracts())
        await new_run.call("get_order", case_id="CASE_ONE", order_id="order-1")
        assert len(new_run_session.calls) == 1

    asyncio.run(run())


def test_workflow_handoffs_link_real_consumption_and_failure(tmp_path):
    class Gateway:
        async def call(self, tool, *, case_id, **arguments):
            if tool == "get_order":
                return {
                    "evidence_ref": "ev_test_only_00000000000000000001",
                    "data": {"order_id": "order-1", "order_status": "delivered"},
                }
            raise RuntimeError("Unavailable evidence")

    case = {
        "case_id": "TEST_CASE",
        "policy_version": "TEST_POLICY",
        "customer_request": {"claimed_order_id": "order-1", "claims": []},
    }
    writer = TraceWriter(tmp_path / "trace.jsonl", contracts())
    writer.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(case, Gateway(), writer))
    writer.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")
    events = [json.loads(line) for line in writer.path.read_text().splitlines()]
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    validate_case_consistency(output, events)
    assert any(e.get("decision_code") == "EVIDENCE_INCOMPLETE" for e in events)
    assert len({e["actor"] for e in events}) >= 5
    output["evidence_refs"].append("ev_test_only_00000000000000009999")
    with pytest.raises(ValueError, match="not consumed"):
        validate_case_consistency(output, events)
