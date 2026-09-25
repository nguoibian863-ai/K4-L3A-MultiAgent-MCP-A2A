"""Evidence-led specialists: customer claims define scope, never facts."""

from __future__ import annotations

import asyncio
from typing import Any

from .analysis import EvidenceView, assess
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


class Specialist:
    def __init__(self, actor: str, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.actor, self.gateway, self.trace = actor, gateway, trace

    async def investigate(self, case_id: str, requests: dict) -> dict:
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=self.actor,
            attributes={"tool_count": len(requests)},
        )
        evidence = {}
        failures = 0
        for tool, arguments in requests.items():
            try:
                response = await self.gateway.call(tool, case_id=case_id, **arguments)
            except (RuntimeError, TimeoutError) as exc:
                failures += 1
                evidence[tool] = {"error": type(exc).__name__}
                continue
            evidence[tool] = response
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=self.actor,
                tool_name=tool,
                evidence_refs=[response["evidence_ref"]],
            )
        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=self.actor,
            target="verifier",
            decision_code=(
                "EVIDENCE_INCOMPLETE"
                if failures
                else "EVIDENCE_READY"
                if requests
                else "NO_LOOKUP_NEEDED"
            ),
            evidence_refs=[r["evidence_ref"] for r in evidence.values() if "evidence_ref" in r],
            attributes={"failed_tools": failures},
        )
        return evidence


async def solve_case(case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> dict:
    case_id = case["case_id"]
    request = case.get("customer_request", {})
    order_id = request.get("claimed_order_id")
    arguments = {"order_id": order_id} if order_id else None
    records: dict[str, dict[str, Any]] = {}
    plans = {
        "order_agent": ["get_order", "get_order_items"],
        "payment_agent": ["get_payment_timeline"],
    }
    results = await asyncio.gather(
        *(
            Specialist(actor, gateway, trace).investigate(
                case_id,
                {name: arguments for name in names} if arguments else {},
            )
            for actor, names in plans.items()
        ),
        Specialist("policy_agent", gateway, trace).investigate(
            case_id,
            {
                "get_policy": {"policy_version": case["policy_version"]},
            },
        ),
    )
    for result in results:
        records.update(result)
    topics = {c.get("topic") for c in request.get("claims", [])}
    view = EvidenceView(case, records)
    if arguments and "error" in records.get("get_payment_timeline", {}):
        records.update(
            await Specialist("payment_agent", gateway, trace).investigate(
                case_id,
                {"get_order_payments": arguments},
            )
        )
    refund_activity = any("refund" in e.get("event_type", "") for e in view.payment_events)
    if arguments and (
        topics & {"refund_pending", "refund_failed"}
        or refund_activity
        or view.order.get("order_status") in {"canceled", "unavailable"}
    ):
        records.update(
            await Specialist("payment_agent", gateway, trace).investigate(
                case_id,
                {"get_refund_timeline": arguments},
            )
        )
    preliminary = assess(case, records)
    delivery_topics = {"late_delivery_seller", "late_delivery_logistics", "unsupported_claim"}
    needs_shipment = (
        bool(topics & delivery_topics)
        or not (topics - {"requested_full_refund"})
        or preliminary["assessment"]["primary_issue"]
        in {"insufficient_evidence", "unsupported_claim"}
    )
    # A claim alone cannot skip shipment; independent evidence must suffice.
    records.update(
        await Specialist("shipment_agent", gateway, trace).investigate(
            case_id,
            {"get_shipment_summary": arguments} if arguments and needs_shipment else {},
        )
    )
    preliminary = assess(case, records)
    if arguments and preliminary["assessment"]["primary_issue"] in {
        "unavailable_order_paid",
        "late_delivery_seller",
    }:
        records.update(
            await Specialist("order_agent", gateway, trace).investigate(
                case_id,
                {"get_sellers": arguments},
            )
        )
    output = assess(case, records)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy_agent",
        target="verifier",
        decision_code=output["assessment"]["primary_issue"].upper(),
        evidence_refs=(
            [records["get_policy"]["evidence_ref"]]
            if "evidence_ref" in records.get("get_policy", {})
            else []
        ),
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="VERIFIED_WITH_GAPS"
        if output["assessment"]["case_status"] == "needs_investigation"
        else "VERIFIED",
        evidence_refs=output["evidence_refs"],
        attributes={"conflict_count": len(output["data_conflicts"])},
    )
    return output
