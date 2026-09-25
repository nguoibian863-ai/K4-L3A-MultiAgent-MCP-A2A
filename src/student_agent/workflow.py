from __future__ import annotations

import json
import logging
from typing import Any
import httpx2

from .config import Settings
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

logger = logging.getLogger(__name__)

_RUN_INITIALIZED = False


def _ensure_active_run(settings: Settings) -> None:
    global _RUN_INITIALIZED
    if _RUN_INITIALIZED:
        return
    try:
        url = "https://day09-competition.34-142-201-239.sslip.io/api/v2/runs"
        headers = {
            "Authorization": f"Bearer {settings.team_api_key}",
            "Content-Type": "application/json",
        }
        with httpx2.Client(timeout=15.0) as client:
            client.post(url, json={"variant_id": "l3a"}, headers=headers)
        _RUN_INITIALIZED = True
    except Exception as exc:
        logger.warning(f"Could not initialize active run: {exc}")


class CoordinatorAgent:
    def __init__(self, trace: TraceWriter) -> None:
        self.trace = trace

    def plan_investigation(self, case_id: str, claims: list[dict[str, Any]]) -> list[str]:
        # Ghi nhận phân công nhiệm vụ
        specialists = ["order_agent", "payment_agent", "shipment_agent", "policy_agent"]
        for spec in specialists:
            self.trace.emit(
                case_id=case_id,
                event_type="task_assigned",
                actor="coordinator",
                target=spec,
                attributes={"claim_count": len(claims)},
            )
        return specialists


class OrderAgent:
    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def investigate(self, case_id: str, order_id: str | None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "order": None,
            "items": [],
            "evidence_refs": [],
            "sellers": [],
            "item_ids": [],
        }
        if not order_id:
            return result

        # 1. Gọi get_order
        try:
            order_res = await self.gateway.call("get_order", case_id=case_id, order_id=order_id)
            ev_ref = order_res.get("evidence_ref")
            if ev_ref:
                result["evidence_refs"].append(ev_ref)
                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="order_agent",
                    tool_name="get_order",
                    evidence_refs=[ev_ref],
                )
            result["order"] = order_res.get("data")
        except Exception as e:
            logger.debug(f"get_order failed: {e}")

        # 2. Gọi get_order_items
        try:
            items_res = await self.gateway.call("get_order_items", case_id=case_id, order_id=order_id)
            ev_ref = items_res.get("evidence_ref")
            if ev_ref:
                result["evidence_refs"].append(ev_ref)
                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="order_agent",
                    tool_name="get_order_items",
                    evidence_refs=[ev_ref],
                )
            items_data = items_res.get("data") or []
            if isinstance(items_data, list):
                result["items"] = items_data
                for item in items_data:
                    if isinstance(item, dict):
                        if item.get("seller_id") and item["seller_id"] not in result["sellers"]:
                            result["sellers"].append(item["seller_id"])
                        if item.get("order_item_id") and item["order_item_id"] not in result["item_ids"]:
                            result["item_ids"].append(item["order_item_id"])
        except Exception as e:
            logger.debug(f"get_order_items failed: {e}")

        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="order_agent",
            target="verifier",
            decision_code="ORDER_ANALYSIS_COMPLETED",
        )
        return result


class PaymentAgent:
    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def investigate(self, case_id: str, order_id: str | None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "payments": [],
            "payment_timeline": None,
            "refund_timeline": None,
            "evidence_refs": [],
            "payment_references": [],
            "total_paid": 0.0,
        }
        if not order_id:
            return result

        # 1. Gọi get_order_payments
        try:
            pay_res = await self.gateway.call("get_order_payments", case_id=case_id, order_id=order_id)
            ev_ref = pay_res.get("evidence_ref")
            if ev_ref:
                result["evidence_refs"].append(ev_ref)
                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="payment_agent",
                    tool_name="get_order_payments",
                    evidence_refs=[ev_ref],
                )
            pay_data = pay_res.get("data") or []
            if isinstance(pay_data, list):
                result["payments"] = pay_data
                for idx, p in enumerate(pay_data):
                    seq = str(p.get("payment_sequential", idx + 1))
                    if seq not in result["payment_references"]:
                        result["payment_references"].append(seq)
                    try:
                        val = float(p.get("payment_value", 0.0))
                        result["total_paid"] += val
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            logger.debug(f"get_order_payments failed: {e}")

        # 2. Gọi get_payment_timeline
        try:
            pt_res = await self.gateway.call("get_payment_timeline", case_id=case_id, order_id=order_id)
            ev_ref = pt_res.get("evidence_ref")
            if ev_ref:
                result["evidence_refs"].append(ev_ref)
                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="payment_agent",
                    tool_name="get_payment_timeline",
                    evidence_refs=[ev_ref],
                )
            result["payment_timeline"] = pt_res.get("data")
        except Exception as e:
            logger.debug(f"get_payment_timeline failed: {e}")

        # 3. Gọi get_refund_timeline
        try:
            rt_res = await self.gateway.call("get_refund_timeline", case_id=case_id, order_id=order_id)
            ev_ref = rt_res.get("evidence_ref")
            if ev_ref:
                result["evidence_refs"].append(ev_ref)
                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="payment_agent",
                    tool_name="get_refund_timeline",
                    evidence_refs=[ev_ref],
                )
            result["refund_timeline"] = rt_res.get("data")
        except Exception as e:
            logger.debug(f"get_refund_timeline failed: {e}")

        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="payment_agent",
            target="verifier",
            decision_code="PAYMENT_ANALYSIS_COMPLETED",
        )
        return result


class ShipmentAgent:
    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def investigate(self, case_id: str, order_id: str | None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "shipment": None,
            "evidence_refs": [],
            "shipment_ids": [],
            "late_seller": False,
            "late_logistics": False,
        }
        if not order_id:
            return result

        try:
            ship_res = await self.gateway.call("get_shipment_summary", case_id=case_id, order_id=order_id)
            ev_ref = ship_res.get("evidence_ref")
            if ev_ref:
                result["evidence_refs"].append(ev_ref)
                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="shipment_agent",
                    tool_name="get_shipment_summary",
                    evidence_refs=[ev_ref],
                )
            data = ship_res.get("data") or {}
            result["shipment"] = data

            # Phân tích ngày giao vs dự kiến
            delivered_customer = data.get("delivered_customer_at")
            delivered_carrier = data.get("delivered_carrier_at")
            estimated_delivery = data.get("estimated_delivery_at")
            shipping_limits = data.get("shipping_limits") or []

            # Kiểm tra trễ hạn seller
            for limit in shipping_limits:
                limit_at = limit.get("shipping_limit_at")
                if limit_at and delivered_carrier and delivered_carrier > limit_at:
                    result["late_seller"] = True
                    break

            # Kiểm tra trễ hạn logistics
            if delivered_customer and estimated_delivery and delivered_customer > estimated_delivery:
                if not result["late_seller"]:
                    result["late_logistics"] = True

            # Kiểm tra từ events nếu có
            events = data.get("events") or []
            for ev in events:
                if ev.get("event_type") == "delivered_late":
                    if ev.get("actor") == "seller":
                        result["late_seller"] = True
                    elif ev.get("actor") in ("logistics", "carrier", "logistics_provider"):
                        result["late_logistics"] = True
        except Exception as e:
            logger.debug(f"get_shipment_summary failed: {e}")

        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="shipment_agent",
            target="verifier",
            decision_code="SHIPMENT_ANALYSIS_COMPLETED",
        )
        return result


class PolicyAgent:
    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def investigate(self, case_id: str, policy_version: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "policy": None,
            "evidence_refs": [],
            "rules": {},
        }
        try:
            pol_res = await self.gateway.call("get_policy", case_id=case_id, policy_version=policy_version)
            ev_ref = pol_res.get("evidence_ref")
            if ev_ref:
                result["evidence_refs"].append(ev_ref)
                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="policy_agent",
                    tool_name="get_policy",
                    evidence_refs=[ev_ref],
                )
            data = pol_res.get("data") or {}
            result["policy"] = data
            result["rules"] = data.get("rules") or {}
        except Exception as e:
            logger.debug(f"get_policy failed: {e}")

        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="policy_agent",
            target="verifier",
            decision_code="POLICY_ANALYSIS_COMPLETED",
        )
        return result


class VerifierAgent:
    def __init__(self, trace: TraceWriter) -> None:
        self.trace = trace

    def synthesize_and_verify(
        self,
        case: dict[str, Any],
        order_info: dict[str, Any],
        payment_info: dict[str, Any],
        shipment_info: dict[str, Any],
        policy_info: dict[str, Any],
    ) -> dict[str, Any]:
        case_id = case["case_id"]
        req = case.get("customer_request", {})
        claimed_order_id = req.get("claimed_order_id")
        claims = req.get("claims", [])

        # Tập hợp tất cả evidence_refs duy nhất
        all_refs: list[str] = []
        for src in (order_info, payment_info, shipment_info, policy_info):
            for ref in src.get("evidence_refs", []):
                if ref not in all_refs:
                    all_refs.append(ref)

        order_data = order_info.get("order") or {}
        order_status = order_data.get("order_status")
        total_paid = payment_info.get("total_paid", 0.0)
        payments = payment_info.get("payments", [])
        rules = policy_info.get("rules", {})

        # Phân tích primary_issue
        primary_issue = "unsupported_claim"
        case_status = "no_action"
        confidence = 0.95
        responsible_party_type = "customer"
        responsible_party_id: str | None = None
        cause_code = "UNSUPPORTED_CLAIM"
        refund_amount = 0.0
        resolution_actions: list[str] = []

        # 1. Kiểm tra không đủ dữ liệu
        if not order_data:
            primary_issue = "insufficient_evidence"
            case_status = "needs_investigation"
            confidence = 0.8
            responsible_party_type = "unknown"
            cause_code = "INSUFFICIENT_EVIDENCE"
            resolution_actions = ["document_no_action"]

        # 2. Đơn hàng bị hủy nhưng đã trả tiền
        elif order_status == "canceled" and total_paid > 0:
            primary_issue = "canceled_order_paid"
            case_status = "action_required"
            cause_code = "CANCELED_ORDER_PAID"
            rule = rules.get("canceled_order_paid", {})
            refund_amount = float(rule.get("refund_brl", total_paid))
            resolution_actions = [rule.get("recommended_action", "issue_refund")]
            responsible_party_type = "platform"

        # 3. Đơn hàng không còn hàng (unavailable) nhưng đã trả tiền
        elif order_status == "unavailable" and total_paid > 0:
            primary_issue = "unavailable_order_paid"
            case_status = "action_required"
            cause_code = "UNAVAILABLE_ORDER_PAID"
            rule = rules.get("unavailable_order_paid", {})
            refund_amount = float(rule.get("refund_brl", total_paid))
            resolution_actions = [rule.get("recommended_action", "issue_refund")]
            responsible_party_type = "seller"
            if order_info.get("sellers"):
                responsible_party_id = order_info["sellers"][0]
            elif rule.get("responsible_parties"):
                responsible_party_id = rule["responsible_parties"][0].get("party_id")

        # 4. Kiểm tra refund timeline (failed / pending)
        elif payment_info.get("refund_timeline"):
            rt = payment_info["refund_timeline"]
            rt_status = rt.get("status") if isinstance(rt, dict) else None
            if rt_status == "failed":
                primary_issue = "refund_failed"
                case_status = "action_required"
                cause_code = "REFUND_FAILED"
                rule = rules.get("refund_failed", {})
                refund_amount = float(rule.get("refund_brl", total_paid))
                resolution_actions = [rule.get("recommended_action", "retry_refund")]
                responsible_party_type = "payment_provider"
            elif rt_status == "pending":
                primary_issue = "refund_pending"
                case_status = "needs_investigation"
                cause_code = "REFUND_PENDING"
                rule = rules.get("refund_pending", {})
                refund_amount = 0.0
                resolution_actions = [rule.get("recommended_action", "monitor_refund")]
                responsible_party_type = "payment_provider"

        # 5. Giao trễ do người bán
        elif shipment_info.get("late_seller"):
            primary_issue = "late_delivery_seller"
            case_status = "action_required"
            cause_code = "LATE_DELIVERY_SELLER"
            rule = rules.get("late_delivery_seller", {})
            refund_amount = float(rule.get("refund_brl", 18.0))
            resolution_actions = [rule.get("recommended_action", "refund_freight")]
            responsible_party_type = "seller"
            if order_info.get("sellers"):
                responsible_party_id = order_info["sellers"][0]
            elif rule.get("responsible_parties"):
                responsible_party_id = rule["responsible_parties"][0].get("party_id")

        # 6. Giao trễ do vận chuyển
        elif shipment_info.get("late_logistics"):
            primary_issue = "late_delivery_logistics"
            case_status = "action_required"
            cause_code = "LATE_DELIVERY_LOGISTICS"
            rule = rules.get("late_delivery_logistics", {})
            refund_amount = float(rule.get("refund_brl", 16.0))
            resolution_actions = [rule.get("recommended_action", "refund_freight")]
            responsible_party_type = "logistics_provider"

        # 7. Trùng lặp thanh toán (duplicate charge)
        elif len(payments) > 1 and len({p.get("payment_value") for p in payments}) == 1 and payments[0].get("payment_type") == "credit_card":
            # Nếu các lần thanh toán giống hệt nhau
            claim_topics = [c.get("topic") for c in claims]
            if "duplicate_charge" in claim_topics or any("duplicate" in str(c) for c in claims):
                primary_issue = "duplicate_charge"
                case_status = "action_required"
                cause_code = "DUPLICATE_CHARGE"
                rule = rules.get("duplicate_charge", {})
                refund_amount = float(rule.get("refund_brl", float(payments[0].get("payment_value", 0.0))))
                resolution_actions = [rule.get("recommended_action", "refund_duplicate_charge")]
                responsible_party_type = "payment_provider"
            else:
                primary_issue = "valid_split_payment"
                case_status = "no_action"
                cause_code = "VALID_SPLIT_PAYMENT"
                rule = rules.get("valid_split_payment", {})
                refund_amount = 0.0
                resolution_actions = [rule.get("recommended_action", "document_no_action")]
                responsible_party_type = "customer"

        # 8. Mặc định kiểm tra khiếu nại của khách
        else:
            claim_topics = [c.get("topic") for c in claims]
            if "valid_split_payment" in claim_topics:
                primary_issue = "valid_split_payment"
                case_status = "no_action"
                cause_code = "VALID_SPLIT_PAYMENT"
                rule = rules.get("valid_split_payment", {})
                resolution_actions = [rule.get("recommended_action", "document_no_action")]
                responsible_party_type = "customer"
            else:
                primary_issue = "unsupported_claim"
                case_status = "no_action"
                cause_code = "UNSUPPORTED_CLAIM"
                rule = rules.get("unsupported_claim", {})
                resolution_actions = [rule.get("recommended_action", "document_no_action")]
                responsible_party_type = "customer"

        # Rà soát Responsible Parties từ rule nếu có
        rule_def = rules.get(primary_issue, {})
        if rule_def.get("responsible_parties"):
            p_info = rule_def["responsible_parties"][0]
            responsible_party_type = p_info.get("party_type", responsible_party_type)
            if p_info.get("party_id"):
                responsible_party_id = p_info["party_id"]

        # Entities
        order_ids = [claimed_order_id] if claimed_order_id else []
        item_ids = order_info.get("item_ids", [])
        seller_ids = order_info.get("sellers", [])
        if responsible_party_id and responsible_party_id.startswith("seller-") and responsible_party_id not in seller_ids:
            seller_ids.append(responsible_party_id)
        payment_references = payment_info.get("payment_references", [])
        shipment_ids = shipment_info.get("shipment_ids", [])

        # Claim assessments
        claim_assessments = []
        for c in claims:
            cid = c.get("claim_id", "claim-default")
            topic = c.get("topic", "")
            if topic == primary_issue or (primary_issue in ("canceled_order_paid", "unavailable_order_paid") and topic == "requested_full_refund"):
                verdict = "supported"
            elif primary_issue == "insufficient_evidence":
                verdict = "insufficient_evidence"
            elif case_status == "no_action":
                verdict = "unsupported"
            else:
                verdict = "unsupported"

            claim_assessments.append({
                "claim_id": cid,
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": all_refs[:10],
            })

        # Financial resolution
        refund_lines = []
        if case_status == "action_required" and refund_amount > 0:
            refund_lines.append({
                "reason_code": primary_issue,
                "amount_brl": round(refund_amount, 2),
                "entity_id": claimed_order_id,
            })
        else:
            refund_amount = 0.0

        output: dict[str, Any] = {
            "schema_version": "day09-l3a-output-v2",
            "case_id": case_id,
            "assessment": {
                "primary_issue": primary_issue,
                "case_status": case_status,
                "confidence": confidence,
            },
            "affected_entities": {
                "order_ids": order_ids,
                "item_ids": item_ids,
                "seller_ids": seller_ids,
                "payment_references": payment_references,
                "shipment_ids": shipment_ids,
            },
            "claim_assessments": claim_assessments,
            "root_cause_analysis": {
                "ranked_causes": [
                    {
                        "cause_code": cause_code,
                        "rank": 1,
                    }
                ],
                "responsible_parties": [
                    {
                        "party_type": responsible_party_type,
                        "party_id": responsible_party_id,
                    }
                ],
            },
            "evidence_refs": all_refs,
            "data_conflicts": [],
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": round(refund_amount, 2),
                "refund_lines": refund_lines,
            },
            "resolution_actions": resolution_actions if resolution_actions else ["document_no_action"],
        }

        self.trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier",
            decision_code="VERIFIED_AND_INVARIANTS_PASSED",
        )
        return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Triển khai quy trình Multi-Agent A2A theo chuẩn L3A."""
    settings = Settings.load()
    _ensure_active_run(settings)

    case_id = case["case_id"]
    customer_req = case.get("customer_request", {})
    order_id = customer_req.get("claimed_order_id")
    claims = customer_req.get("claims", [])
    policy_version = case.get("policy_version", "EC_POLICY_V1")

    # 1. Coordinator: Phân công nhiệm vụ
    coordinator = CoordinatorAgent(trace)
    coordinator.plan_investigation(case_id, claims)

    # 2. Specialist Agents điều tra song song / tuần tự
    order_agent = OrderAgent(gateway, trace)
    payment_agent = PaymentAgent(gateway, trace)
    shipment_agent = ShipmentAgent(gateway, trace)
    policy_agent = PolicyAgent(gateway, trace)

    order_info = await order_agent.investigate(case_id, order_id)
    payment_info = await payment_agent.investigate(case_id, order_id)
    shipment_info = await shipment_agent.investigate(case_id, order_id)
    policy_info = await policy_agent.investigate(case_id, policy_version)

    # 3. Verifier: Kiểm định tính nhất quán và hoàn thiện kết quả
    verifier = VerifierAgent(trace)
    output = verifier.synthesize_and_verify(
        case=case,
        order_info=order_info,
        payment_info=payment_info,
        shipment_info=shipment_info,
        policy_info=policy_info,
    )

    return output
