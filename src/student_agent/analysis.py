"""Deterministic reasoning over scoped MCP evidence (no network or label lookup)."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0.00")


def money(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result.quantize(Decimal("0.01")) if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        return None


def rows(value: Any, key: str) -> list[dict[str, Any]]:
    value = value.get(key, []) if isinstance(value, dict) else value
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def unique(values: Any) -> list[str]:
    return list(dict.fromkeys(str(v) for v in values if v is not None and str(v)))


class EvidenceView:
    def __init__(self, case: dict, records: dict) -> None:
        self.case, self.records = case, records
        self.order_id = case.get("customer_request", {}).get("claimed_order_id")
        self.conflicts: list[dict] = []
        self.order = self.object("get_order")
        self.shipment = self.object("get_shipment_summary")
        self.policy = self.object("get_policy")
        self.items = self.scoped(rows(self.data("get_order_items"), "items"))
        timeline = self.object("get_payment_timeline")
        self.payments = self.scoped(rows(timeline, "payments"))
        if not self.payments:
            self.payments = self.scoped(rows(self.data("get_order_payments"), "payments"))
        self.payment_events = self.scoped(rows(timeline, "events"))
        self.shipment_events = self.scoped(rows(self.shipment, "events"))
        self.refund_events = self.scoped(rows(self.data("get_refund_timeline"), "events"))
        self.order_current = True
        self.window_anchored = False
        self._resolve_versions()
        self._resolve_shipment_events()

    def data(self, tool: str) -> Any:
        return self.records.get(tool, {}).get("data")

    def object(self, tool: str) -> dict:
        data = self.data(tool)
        if isinstance(data, dict) and data.get("order_id") not in (None, self.order_id):
            raise ValueError("MCP returned a different order")
        return data if isinstance(data, dict) else {}

    def available(self, tool: str) -> bool:
        return "evidence_ref" in self.records.get(tool, {})

    def refs(self, *tools: str) -> list[str]:
        return unique(self.records[t]["evidence_ref"] for t in tools if self.available(t))

    def scoped(self, records: list[dict]) -> list[dict]:
        for record in records:
            if record.get("order_id") not in (None, self.order_id):
                raise ValueError("MCP returned an entity from a different order")
        result = []
        for record in records:
            if record not in result:
                result.append(record)
        return result

    def conflict(self, field: str, sources: list[str], selected: str | None, code: str) -> None:
        value = {
            "field": field,
            "sources": sources,
            "selected_source": selected,
            "resolution_code": code,
        }
        if value not in self.conflicts:
            self.conflicts.append(value)

    def _resolve_versions(self) -> None:
        """Resolve repeated item IDs with conflicting dates using case observation time.

        Ordinary multi-payment orders are never divided into episodes. This rule applies
        only to version-colliding item rows and multiple dated capture groups.
        """
        groups: dict[str, list[dict]] = defaultdict(list)
        for item in self.items:
            groups[str(item.get("order_item_id"))].append(item)
        collisions = any(
            len({r.get("shipping_limit_date") for r in group}) > 1 for group in groups.values()
        )
        opened = timestamp(self.case.get("opened_at"))
        capture_dates = sorted(
            {
                dt.date()
                for e in self.payment_events
                if e.get("event_type") == "captured" and (dt := timestamp(e.get("event_at")))
            }
        )
        if not collisions or not opened or len(capture_dates) < 2:
            return
        past = [day for day in capture_dates if day <= opened.date()]
        if not past:
            return
        purchased = timestamp(self.order.get("order_purchase_timestamp"))
        # A later capture is not necessarily a new version of this case's order.
        # Prefer an authoritative purchase date that has a matching capture group.
        self.window_anchored = purchased is not None and purchased.date() in past
        start = purchased.date() if self.window_anchored else past[-1]
        end = next((day for day in capture_dates if day > start), None)

        capture_groups: dict[Any, set[Decimal]] = defaultdict(set)
        for event in self.payment_events:
            event_time = timestamp(event.get("event_at"))
            amount = money(event.get("amount_brl"))
            if event.get("event_type") == "captured" and event_time and amount is not None:
                capture_groups[event_time.date()].add(amount)
        active_amounts = capture_groups[start]
        other_amounts = set().union(*(v for day, v in capture_groups.items() if day != start))

        def in_episode(row: dict, field: str = "event_at") -> bool:
            dt = timestamp(row.get(field))
            return dt is not None and dt.date() >= start and (end is None or dt.date() < end)

        self.payment_events = [e for e in self.payment_events if in_episode(e)]
        # A refund may arrive after another capture group began. A unique amount
        # ties it back to the earlier transaction; time boundaries alone lose it.
        self.refund_events = [
            e
            for e in self.refund_events
            if in_episode(e)
            or (
                (amount := money(e.get("amount_brl"))) in active_amounts - other_amounts
                and (event_time := timestamp(e.get("event_at")))
                and event_time.date() >= start
                and event_time <= opened
            )
        ]
        delivered = timestamp(self.shipment.get("delivered_customer_at"))
        self.shipment_events = [
            e
            for e in self.shipment_events
            if in_episode(e)
            or (
                self.window_anchored
                and delivered is not None
                and timestamp(e.get("event_at")) == delivered
            )
        ]
        self.items = [
            r for group in groups.values() for r in group if in_episode(r, "shipping_limit_date")
        ]
        capture_amounts = {
            money(e.get("amount_brl"))
            for e in self.payment_events
            if e.get("event_type") == "captured"
        }
        self.payments = [
            p for p in self.payments if money(p.get("payment_value")) in capture_amounts
        ]
        self.order_current = purchased is not None and purchased.date() == start
        self.conflict(
            "order_item_id",
            ["get_order_items", "get_payment_timeline"],
            "get_order" if self.window_anchored else "get_payment_timeline",
            "SELECT_ORDER_PURCHASE_WINDOW" if self.window_anchored else "SELECT_CASE_TIME_WINDOW",
        )
        if not self.order_current:
            self.conflict(
                "order_status",
                ["get_order", "get_payment_timeline"],
                None,
                "ORDER_SNAPSHOT_OUTSIDE_CASE_TIME_WINDOW",
            )

    def _resolve_shipment_events(self) -> None:
        delivered = timestamp(self.shipment.get("delivered_customer_at"))
        order_delivered = timestamp(self.order.get("order_delivered_customer_date"))
        # Use the newer delivery snapshot only when both authoritative views agree.
        # Keep unknown timestamps and unresolved disagreements for verification.
        if not self.order_current or delivered is None or delivered != order_delivered:
            return
        obsolete = [
            e
            for e in self.shipment_events
            if e.get("event_type") == "delivered_late"
            and (event_time := timestamp(e.get("event_at")))
            and event_time < delivered
        ]
        if obsolete:
            self.shipment_events = [e for e in self.shipment_events if e not in obsolete]
            self.conflict(
                "delivered_customer_at",
                ["get_order", "get_shipment_summary"],
                "get_order",
                "SELECT_NEWER_CONFIRMED_DELIVERY",
            )


def assess(case: dict, records: dict) -> dict:
    view = EvidenceView(case, records)
    topics = {c.get("topic") for c in case.get("customer_request", {}).get("claims", [])}
    rules = view.policy.get("rules", {})
    candidates: dict[str, tuple[float, list[str]]] = {}
    base_tools = ["get_order", "get_policy"]
    payment_tools = (
        ["get_payment_timeline"]
        if view.available("get_payment_timeline")
        else ["get_order_payments"]
    )
    captured = [
        e
        for e in view.payment_events
        if e.get("event_type") == "captured"
        and e.get("status") in {"confirmed", "completed", "succeeded", "settled"}
    ]
    amounts = [money(e.get("amount_brl")) for e in captured]
    payment_amounts = [money(p.get("payment_value")) for p in view.payments]
    paid = sum((a for a in (amounts if captured else payment_amounts) if a is not None), ZERO)
    item_totals = [money(i.get("price")) for i in view.items]
    freight = [money(i.get("freight_value")) for i in view.items]
    expected = (
        sum(item_totals, ZERO) + sum(freight, ZERO)
        if item_totals and None not in item_totals + freight
        else None
    )

    def add(issue: str, confidence: float, tools: list[str]) -> None:
        candidates[issue] = (confidence, unique(base_tools + tools))

    refund_events = sorted(
        view.refund_events,
        key=lambda e: timestamp(e.get("event_at")) or datetime.min.replace(tzinfo=UTC),
    )
    refund = refund_events[-1] if refund_events else {}
    refund_status = refund.get("status") or view.object("get_refund_timeline").get("status")
    if refund_status == "failed":
        add("refund_failed", 0.96, payment_tools + ["get_refund_timeline"])
    elif refund_status in {"pending", "processing", "requested"}:
        add("refund_pending", 0.96, payment_tools + ["get_refund_timeline"])

    if (
        view.order_current
        and paid > ZERO
        and refund_status not in {"completed", "succeeded", "refunded", "settled"}
    ):
        status = view.order.get("order_status")
        if status in {"canceled", "unavailable"}:
            add(
                f"{status}_order_paid",
                0.95,
                payment_tools
                + (["get_order_items", "get_sellers"] if status == "unavailable" else []),
            )

    mismatch = [
        e
        for e in view.payment_events
        if e.get("event_type") in {"reconciliation_mismatch", "payment_mismatch"}
        and e.get("status") not in {"resolved", "closed", "reversed"}
    ]
    duplicate = [
        e
        for e in view.payment_events
        if e.get("event_type") in {"duplicate_charge", "duplicate_capture"}
        and e.get("status") not in {"reversed", "resolved", "voided"}
    ]
    if mismatch:
        add("payment_mismatch", 0.96, payment_tools + ["get_order_items"])
    if duplicate:
        add("duplicate_charge", 0.96, payment_tools)
    elif (
        expected is not None
        and len(captured) > 1
        and None not in amounts
        and len(set(amounts)) == 1
        and paid > expected
    ):
        add("duplicate_charge", 0.78, payment_tools + ["get_order_items"])
    elif (
        expected is not None
        and len(view.payments) > 1
        and abs(paid - expected) <= Decimal("0.01")
        and len(unique(p.get("payment_sequential") for p in view.payments)) > 1
    ):
        add("valid_split_payment", 0.94, payment_tools + ["get_order_items"])

    late_events = [
        e
        for e in view.shipment_events
        if e.get("event_type") == "delivered_late" and e.get("status") == "confirmed"
    ]
    late_actors = {e.get("actor") for e in late_events}
    seller_ids = unique(i.get("seller_id") for i in view.items)
    late_sellers = []
    delivered = timestamp(view.shipment.get("delivered_customer_at"))
    estimated = timestamp(view.shipment.get("estimated_delivery_at"))
    carrier = timestamp(view.shipment.get("delivered_carrier_at"))
    late = bool(delivered and estimated and delivered > estimated and view.order_current)
    if late:
        late_sellers = unique(
            i.get("seller_id")
            for i in view.items
            if carrier and (limit := timestamp(i.get("shipping_limit_date"))) and carrier > limit
        )
    if late_actors == {"seller"} or (late and late_sellers and not late_actors):
        add(
            "late_delivery_seller", 0.94, ["get_order_items", "get_shipment_summary", "get_sellers"]
        )
    elif late_actors & {"logistics_provider", "logistics", "carrier"} or (
        late and not late_sellers
    ):
        add("late_delivery_logistics", 0.94, ["get_order_items", "get_shipment_summary"])
    if len(late_actors) > 1:
        view.conflict(
            "shipment.responsibility",
            ["shipment.events", "shipment.timestamps"],
            None,
            "CONFLICTING_SHIPMENT_ACTORS",
        )

    required_missing = (
        not view.order
        or not view.available("get_policy")
        or not any(view.available(t) for t in payment_tools)
    )
    refund_unavailable = bool(topics & {"refund_pending", "refund_failed"}) and not view.available(
        "get_refund_timeline"
    )
    precedence = [
        "refund_failed",
        "refund_pending",
        "duplicate_charge",
        "payment_mismatch",
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "valid_split_payment",
    ]
    scoped = [issue for issue in precedence if issue in candidates and issue in topics]
    matched = scoped or [issue for issue in precedence if issue in candidates]
    if required_missing or refund_unavailable:
        issue, confidence = "insufficient_evidence", 0.45
        used = (
            base_tools
            + payment_tools
            + ["get_order_items", "get_shipment_summary", "get_refund_timeline"]
        )
    elif matched:
        issue = matched[0]
        confidence, used = candidates[issue]
        used = list(used)
        if len(matched) > 1:
            confidence = min(confidence, 0.75)
    elif not view.order_current or not view.available("get_shipment_summary"):
        issue, confidence = "insufficient_evidence", 0.45
        used = base_tools + payment_tools + ["get_order_items", "get_shipment_summary"]
    else:
        issue, confidence = "unsupported_claim", 0.85
        used = base_tools + payment_tools + ["get_order_items", "get_shipment_summary"]

    rule = rules.get(issue)
    if issue != "insufficient_evidence" and not isinstance(rule, dict):
        issue, confidence, rule = "insufficient_evidence", 0.4, {}
    rule = rule or {}
    if view.conflicts:
        resolved_codes = {"SELECT_ORDER_PURCHASE_WINDOW", "SELECT_NEWER_CONFIRMED_DELIVERY"}
        resolved = all(c["resolution_code"] in resolved_codes for c in view.conflicts)
        confidence = min(confidence, 0.92 if resolved else 0.8 if view.order_current else 0.65)
    if any(r.get("warnings") for r in records.values()):
        confidence = min(confidence, 0.8)
    if len(late_actors) > 1 and issue.startswith("late_delivery"):
        issue, confidence, rule = "insufficient_evidence", 0.4, {}
    status = rule.get("case_status", "needs_investigation")
    if status not in {"action_required", "no_action", "needs_investigation"}:
        raise ValueError("Policy returned an unsupported case_status")
    refund_amount = money(rule.get("refund_brl", 0))
    if refund_amount is None or refund_amount < ZERO:
        raise ValueError("Policy returned an invalid refund amount")
    if status != "action_required":
        refund_amount = ZERO
    action = rule.get("recommended_action", "request_additional_evidence")
    parties = []
    for party in rule.get("responsible_parties", []):
        party = dict(party)
        if party.get("party_type") == "seller":
            scoped_sellers = (
                late_sellers if issue == "late_delivery_seller" and late_sellers else (seller_ids)
            )
            parties.extend({"party_type": "seller", "party_id": s} for s in scoped_sellers)
            if not scoped_sellers:
                parties.append({"party_type": "seller", "party_id": None})
        else:
            parties.append(party)
    if not parties:
        parties = [{"party_type": "unknown", "party_id": None}]
    if len(parties) > 5:
        raise ValueError("Too many responsible parties for the output contract")

    used += ["get_order_items"] + payment_tools
    # Conflict resolution and completed-refund decisions also need their evidence,
    # including evidence used to rule out an otherwise eligible refund.
    for conflict in view.conflicts:
        used += [source for source in conflict["sources"] if source in records]
    if refund_status in {"completed", "succeeded", "refunded", "settled"}:
        used += ["get_refund_timeline"]
    assessments = []
    for claim in case.get("customer_request", {}).get("claims", []):
        topic = claim.get("topic")
        claim_tools = list(used)
        if issue == "insufficient_evidence":
            verdict, claim_confidence = "insufficient_evidence", confidence
        elif topic == "requested_full_refund":
            verdict = (
                "supported"
                if issue in {"canceled_order_paid", "unavailable_order_paid"}
                else "partially_supported"
                if refund_amount > ZERO
                else "unsupported"
            )
            claim_confidence = confidence
        elif topic in candidates:
            verdict, claim_confidence = "supported", min(confidence, candidates[topic][0])
            claim_tools = candidates[topic][1]
        elif topic == "unsupported_claim" and issue == topic:
            verdict, claim_confidence = "supported", confidence
        else:
            verdict, claim_confidence = "unsupported", confidence
        refs = view.refs(*claim_tools)
        assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": claim_confidence,
                "evidence_refs": refs,
            }
        )
        used += claim_tools
    refs = unique(view.refs(*used) + [r for c in assessments for r in c["evidence_refs"]])
    if not refs:
        raise RuntimeError("No audited evidence: refusing to produce a scorable-looking output")
    entities = {
        "order_ids": [view.order_id] if view.order and view.order_id else [],
        "item_ids": unique(i.get("order_item_id") for i in view.items),
        "seller_ids": seller_ids,
        "payment_references": unique(
            p.get("payment_reference", p.get("payment_sequential")) for p in view.payments
        ),
        "shipment_ids": unique(
            [view.shipment.get("shipment_id")]
            + [e.get("shipment_id") for e in view.shipment_events]
        ),
    }
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case["case_id"],
        "assessment": {"primary_issue": issue, "case_status": status, "confidence": confidence},
        "affected_entities": entities,
        "claim_assessments": assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": refs,
        "data_conflicts": view.conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(refund_amount),
            "refund_lines": [
                {
                    "reason_code": issue,
                    "amount_brl": float(refund_amount),
                    "entity_id": view.order_id,
                }
            ]
            if refund_amount > ZERO
            else [],
        },
        "resolution_actions": [action],
    }
