from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

_COORDINATOR = "coordinator"
_ORDER_AGENT = "order-agent"
_PAYMENT_AGENT = "payment-agent"
_SHIPMENT_AGENT = "shipment-agent"
_POLICY_AGENT = "policy-agent"
_VERIFIER = "verifier"

_CORE_EVIDENCE = ("order", "items", "payment_timeline", "policy")
_RELEVANT_EVIDENCE: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": _CORE_EVIDENCE,
    "unavailable_order_paid": (*_CORE_EVIDENCE, "sellers"),
    "late_delivery_seller": (*_CORE_EVIDENCE, "shipment", "sellers"),
    "late_delivery_logistics": (*_CORE_EVIDENCE, "shipment"),
    "payment_mismatch": (*_CORE_EVIDENCE, "payments"),
    "duplicate_charge": (*_CORE_EVIDENCE, "payments"),
    "valid_split_payment": (*_CORE_EVIDENCE, "payments"),
    "refund_pending": (*_CORE_EVIDENCE, "refund_timeline"),
    "refund_failed": (*_CORE_EVIDENCE, "refund_timeline"),
    "unsupported_claim": (*_CORE_EVIDENCE, "shipment"),
}

_BASE_CONFIDENCE = 0.97


def _amount(value: str) -> float:
    return round(float(value), 2)


def _at(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _dedupe(records: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for record in records:
        signature = tuple(record.get(key) for key in keys)
        if signature not in seen:
            seen.add(signature)
            result.append(record)
    return result


def _canonical_items(
    items_raw: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items_raw:
        groups[item["order_item_id"]].append(item)
    canonical: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for records in groups.values():
        by_shipping_limit = sorted(records, key=lambda r: r["shipping_limit_date"])
        chosen = by_shipping_limit[0]
        canonical.append(chosen)
        if len({r["freight_value"] for r in records}) > 1:
            conflicts.append(
                {
                    "field": "freight_value",
                    "sources": [
                        f"shipping_limit_date={r['shipping_limit_date']}:"
                        f"freight_value={r['freight_value']}"
                        for r in by_shipping_limit
                    ],
                    "selected_source": (
                        f"shipping_limit_date={chosen['shipping_limit_date']}:"
                        f"freight_value={chosen['freight_value']}"
                    ),
                    "resolution_code": "earliest_shipping_limit_selected",
                }
            )
    return canonical, conflicts


def _item_total(canonical_items: list[dict[str, Any]]) -> float:
    return round(
        sum(_amount(i["price"]) + _amount(i["freight_value"]) for i in canonical_items), 2
    )


def _lifecycle(
    order: dict[str, Any],
    timeline: dict[str, Any],
    shipment: dict[str, Any],
    refund_events: list[dict[str, Any]],
) -> dict[str, Any]:
    purchase_at = _at(order["order_purchase_timestamp"])
    delivered_at = _at(shipment.get("delivered_customer_at"))
    estimated_at = _at(shipment["estimated_delivery_at"])
    # Timelines also carry events copied from unrelated scenarios, dated weeks away from this
    # order's lifecycle; only events inside the lifecycle windows count as evidence.
    payment_window_end = purchase_at + timedelta(days=1)
    refund_window_end = max(filter(None, [delivered_at, estimated_at])) + timedelta(days=7)
    payment_events = [
        event
        for event in timeline.get("events", [])
        if purchase_at <= _at(event["event_at"]) < payment_window_end
    ]
    refunds = [
        event
        for event in refund_events
        if event["event_type"] == "refund_requested"
        and purchase_at <= _at(event["event_at"]) <= refund_window_end
    ]
    late_actor = None
    if delivered_at:
        aligned = [
            event
            for event in shipment.get("events", [])
            if event["event_type"] == "delivered_late"
            and abs(_at(event["event_at"]) - delivered_at) <= timedelta(days=1)
        ]
        late_actor = aligned[0]["actor"] if aligned else None
    return {
        "delivered_at": delivered_at,
        "estimated_at": estimated_at,
        "payment_events": payment_events,
        "refunds": refunds,
        "late_actor": late_actor,
        "amounts": [_amount(event["amount_brl"]) for event in payment_events + refunds],
    }


def _classify_before_refunds(
    order_status: str, lifecycle: dict[str, Any], item_total: float
) -> str | None:
    if order_status == "canceled":
        return "canceled_order_paid"
    if order_status == "unavailable":
        return "unavailable_order_paid"

    events = lifecycle["payment_events"]
    if any(event["event_type"] == "reconciliation_mismatch" for event in events):
        return "payment_mismatch"

    captured = [
        _amount(event["amount_brl"]) for event in events if event["event_type"] == "captured"
    ]
    captured_total = round(sum(captured), 2)
    # Equal captures that add up to the order value are legs of one split payment, not a duplicate.
    duplicated = [
        amount
        for amount in set(captured)
        if captured.count(amount) >= 2 and abs(amount * captured.count(amount) - item_total) >= 0.01
    ]
    if duplicated and captured_total > item_total + 0.01:
        return "duplicate_charge"

    delivered_at, estimated_at = lifecycle["delivered_at"], lifecycle["estimated_at"]
    if delivered_at and delivered_at > estimated_at:
        if lifecycle["late_actor"] == "seller":
            return "late_delivery_seller"
        return "late_delivery_logistics"

    if len(captured) >= 2 and abs(captured_total - item_total) < 0.01:
        return "valid_split_payment"

    # Only the refund lifecycle can still tell refund issues apart from an unsupported claim.
    return None


def _classify_refunds(lifecycle: dict[str, Any]) -> str:
    if lifecycle["refunds"]:
        latest = max(lifecycle["refunds"], key=lambda event: _at(event["event_at"]))
        if latest["status"] == "pending":
            return "refund_pending"
        if latest["status"] == "failed":
            return "refund_failed"
    return "unsupported_claim"


async def _consume(
    tool_name: str,
    actor: str,
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    evidence = await gateway.call(tool_name, case_id=case_id, order_id=order_id)
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[evidence["evidence_ref"]],
    )
    return evidence


async def _collect_evidence(
    case_id: str, order_id: str, gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, dict[str, Any] | None]:
    evidence: dict[str, dict[str, Any] | None] = {}

    trace.emit(case_id=case_id, event_type="task_assigned", actor=_COORDINATOR, target=_ORDER_AGENT)
    evidence["order"] = await _consume("get_order", _ORDER_AGENT, case_id, order_id, gateway, trace)
    evidence["items"] = await _consume(
        "get_order_items", _ORDER_AGENT, case_id, order_id, gateway, trace
    )
    evidence["sellers"] = await _consume(
        "get_sellers", _ORDER_AGENT, case_id, order_id, gateway, trace
    )
    trace.emit(case_id=case_id, event_type="handoff", actor=_ORDER_AGENT, target=_PAYMENT_AGENT)

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor=_COORDINATOR, target=_PAYMENT_AGENT
    )
    evidence["payments"] = await _consume(
        "get_order_payments", _PAYMENT_AGENT, case_id, order_id, gateway, trace
    )
    evidence["payment_timeline"] = await _consume(
        "get_payment_timeline", _PAYMENT_AGENT, case_id, order_id, gateway, trace
    )
    trace.emit(case_id=case_id, event_type="handoff", actor=_PAYMENT_AGENT, target=_SHIPMENT_AGENT)

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor=_COORDINATOR, target=_SHIPMENT_AGENT
    )
    evidence["shipment"] = await _consume(
        "get_shipment_summary", _SHIPMENT_AGENT, case_id, order_id, gateway, trace
    )
    return evidence


def _bind_parties(
    policy_parties: list[dict[str, Any]], seller_ids: list[str]
) -> list[dict[str, Any]]:
    # Policy rules name a template seller; responsibility must point at this order's seller.
    bound: list[dict[str, Any]] = []
    for party in policy_parties:
        if party["party_type"] == "seller":
            bound.extend({"party_type": "seller", "party_id": seller} for seller in seller_ids)
            if not seller_ids:
                bound.append({"party_type": "seller", "party_id": None})
        else:
            bound.append({"party_type": party["party_type"], "party_id": party["party_id"]})
    return _dedupe(bound, ("party_type", "party_id"))[:5]


def _refund_claim_verdict(case_status: str, action: str, refund_brl: float) -> str:
    if case_status == "no_action":
        return "unsupported"
    if action == "issue_refund" and refund_brl > 0:
        return "supported"
    return "partially_supported"


def _build_output(
    case_id: str,
    order_id: str,
    primary_issue: str,
    rule: dict[str, Any],
    parties: list[dict[str, Any]],
    canonical_items: list[dict[str, Any]],
    distinct_payments: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    evidence_refs: list[str],
    conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    case_status = rule["case_status"]
    refund_brl = rule["refund_brl"]
    action = rule["recommended_action"]

    claim_assessments = []
    for claim in claims:
        if claim["topic"] == "requested_full_refund":
            verdict = _refund_claim_verdict(case_status, action, refund_brl)
        else:
            verdict = "supported" if claim["topic"] == primary_issue else "unsupported"
        claim_assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": 0.0,
                "evidence_refs": evidence_refs,
            }
        )

    refund_lines = []
    if refund_brl > 0:
        refund_lines.append(
            {"reason_code": primary_issue, "amount_brl": refund_brl, "entity_id": order_id}
        )

    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": 0.0,
        },
        "affected_entities": {
            "order_ids": [order_id],
            "item_ids": sorted({item["order_item_id"] for item in canonical_items}),
            "seller_ids": sorted({item["seller_id"] for item in canonical_items}),
            "payment_references": sorted(
                {f"{p['payment_type']}-{p['payment_sequential']}" for p in distinct_payments}
            ),
            "shipment_ids": [order_id],
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_brl,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [action],
    }


def _verify(output: dict[str, Any], rule: dict[str, Any], consumed_refs: set[str]) -> list[str]:
    failures: list[str] = []
    assessment = output["assessment"]
    financial = output["financial_resolution"]
    refund_total = round(sum(line["amount_brl"] for line in financial["refund_lines"]), 2)

    if abs(refund_total - financial["recommended_refund_brl"]) > 0.01:
        failures.append("refund_lines_total_mismatch")
    if assessment["case_status"] == "no_action" and financial["recommended_refund_brl"] > 0:
        failures.append("refund_without_action")
    if output["resolution_actions"][0] != rule["recommended_action"]:
        failures.append("action_not_from_policy")
    if output["root_cause_analysis"]["ranked_causes"][0]["cause_code"] != assessment[
        "primary_issue"
    ].upper():
        failures.append("root_cause_mismatch")

    seller_ids = set(output["affected_entities"]["seller_ids"])
    for party in output["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] == "seller" and party["party_id"] not in seller_ids:
            failures.append("seller_out_of_scope")

    output_refs = set(output["evidence_refs"])
    if not output_refs or not output_refs <= consumed_refs:
        failures.append("evidence_not_consumed")
    for claim in output["claim_assessments"]:
        if not set(claim["evidence_refs"]) <= output_refs:
            failures.append("claim_evidence_unlinked")
    return failures


def _confidence(
    primary_issue: str, lifecycle: dict[str, Any], refund_brl: float, failures: list[str]
) -> float:
    value = _BASE_CONFIDENCE
    if refund_brl > 0 and not any(abs(a - refund_brl) < 0.01 for a in lifecycle["amounts"]):
        value -= 0.15
    if primary_issue.startswith("late_delivery") and lifecycle["late_actor"] is None:
        value -= 0.15
    value -= 0.3 * len(failures)
    return round(min(0.99, max(0.05, value)), 2)


def _insufficient_evidence_output(
    case_id: str, order_id: str | None, claims: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.1,
        },
        "affected_entities": {
            "order_ids": [order_id] if order_id else [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": [
            {
                "claim_id": claim["claim_id"],
                "verdict": "insufficient_evidence",
                "confidence": 0.1,
                "evidence_refs": [],
            }
            for claim in claims
        ],
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": [],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": ["escalate_to_specialist"],
    }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    request = case["customer_request"]
    order_id = request["claimed_order_id"]
    claims = request["claims"]

    try:
        evidence = await _collect_evidence(case_id, order_id, gateway, trace)
        order = evidence["order"]["data"]
        canonical_items, conflicts = _canonical_items(evidence["items"]["data"])
        timeline = evidence["payment_timeline"]["data"]
        shipment = evidence["shipment"]["data"]
        lifecycle = _lifecycle(order, timeline, shipment, [])
        primary_issue = _classify_before_refunds(
            order["order_status"], lifecycle, _item_total(canonical_items)
        )
        if primary_issue is None:
            # Refund history is only fetched when it can change the decision: for orders without
            # refunds the tool answers with an error, and every call is audited.
            trace.emit(
                case_id=case_id, event_type="handoff", actor=_SHIPMENT_AGENT, target=_PAYMENT_AGENT
            )
            try:
                evidence["refund_timeline"] = await _consume(
                    "get_refund_timeline", _PAYMENT_AGENT, case_id, order_id, gateway, trace
                )
            except RuntimeError:
                evidence["refund_timeline"] = None
            refund_ev = evidence["refund_timeline"]
            lifecycle = _lifecycle(
                order, timeline, shipment, refund_ev["data"]["events"] if refund_ev else []
            )
            primary_issue = _classify_refunds(lifecycle)
            trace.emit(
                case_id=case_id, event_type="handoff", actor=_PAYMENT_AGENT, target=_POLICY_AGENT
            )
        else:
            trace.emit(
                case_id=case_id, event_type="handoff", actor=_SHIPMENT_AGENT, target=_POLICY_AGENT
            )

        trace.emit(
            case_id=case_id, event_type="task_assigned", actor=_COORDINATOR, target=_POLICY_AGENT
        )
        evidence["policy"] = await gateway.call(
            "get_policy", case_id=case_id, policy_version=case["policy_version"]
        )
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=_POLICY_AGENT,
            tool_name="get_policy",
            evidence_refs=[evidence["policy"]["evidence_ref"]],
        )
        rule = evidence["policy"]["data"]["rules"][primary_issue]
        seller_ids = sorted({item["seller_id"] for item in canonical_items})
        parties = _bind_parties(rule["responsible_parties"], seller_ids)
        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor=_POLICY_AGENT,
            decision_code=primary_issue,
            target=rule["recommended_action"],
            attributes={
                "case_status": rule["case_status"],
                "refund_brl": rule["refund_brl"],
                "responsible_party_type": parties[0]["party_type"] if parties else None,
            },
        )
        trace.emit(case_id=case_id, event_type="handoff", actor=_POLICY_AGENT, target=_VERIFIER)

        evidence_refs = [
            evidence[name]["evidence_ref"]
            for name in _RELEVANT_EVIDENCE[primary_issue]
            if evidence.get(name) is not None
        ]
        distinct_payments = _dedupe(
            evidence["payments"]["data"],
            ("payment_sequential", "payment_type", "payment_installments", "payment_value"),
        )
        output = _build_output(
            case_id,
            order_id,
            primary_issue,
            rule,
            parties,
            canonical_items,
            distinct_payments,
            claims,
            evidence_refs,
            conflicts,
        )

        consumed_refs = {ev["evidence_ref"] for ev in evidence.values() if ev is not None}
        failures = _verify(output, rule, consumed_refs)
        confidence = _confidence(primary_issue, lifecycle, rule["refund_brl"], failures)
        output["assessment"]["confidence"] = confidence
        for claim in output["claim_assessments"]:
            claim["confidence"] = confidence
    except (RuntimeError, ValueError, KeyError):
        trace.emit(case_id=case_id, event_type="handoff", actor=_COORDINATOR, target=_VERIFIER)
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor=_VERIFIER,
            decision_code="insufficient_evidence",
        )
        return _insufficient_evidence_output(case_id, order_id, claims)

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor=_VERIFIER,
        decision_code="verified" if not failures else "flagged",
        attributes={
            "primary_issue": primary_issue,
            "checks_failed": len(failures),
            "failed_checks": ",".join(failures) or None,
            "confidence": confidence,
        },
    )
    return output
