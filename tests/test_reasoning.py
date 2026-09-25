from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from student_agent.analysis import assess, timestamp
from student_agent.contracts import Contracts


@pytest.fixture
def scenario():
    case = {
        "case_id": "TEST_CASE",
        "opened_at": "2024-02-15T00:00:00Z",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [{"claim_id": "claim-1", "topic": "requested_full_refund"}],
        },
    }
    specifications = {
        "canceled_order_paid": ("action_required", "issue_refund", 89, "platform"),
        "unavailable_order_paid": ("action_required", "issue_refund", 89, "seller"),
        "late_delivery_seller": ("action_required", "refund_freight", 10, "seller"),
        "late_delivery_logistics": ("action_required", "refund_freight", 10, "logistics_provider"),
        "valid_split_payment": ("no_action", "document_no_action", 0, "customer"),
        "payment_mismatch": ("action_required", "reconcile_payment", 35, "payment_provider"),
        "duplicate_charge": ("action_required", "refund_duplicate_charge", 89, "payment_provider"),
        "refund_pending": ("needs_investigation", "monitor_refund", 0, "payment_provider"),
        "refund_failed": ("action_required", "retry_refund", 52, "payment_provider"),
        "unsupported_claim": ("no_action", "document_no_action", 0, "customer"),
    }
    rules = {
        issue: {
            "case_status": status,
            "recommended_action": action,
            "refund_brl": amount,
            "responsible_parties": [
                {"party_type": party, "party_id": "unrelated-seller" if party == "seller" else None}
            ],
        }
        for issue, (status, action, amount, party) in specifications.items()
    }
    data = {
        "get_order": {
            "order_id": "order-1",
            "order_status": "delivered",
            "order_purchase_timestamp": "2024-02-01T09:00:00Z",
        },
        "get_order_items": [
            {
                "order_id": "order-1",
                "order_item_id": "item-1",
                "seller_id": "seller-1",
                "price": "79.00",
                "freight_value": "10.00",
                "shipping_limit_date": "2024-02-04T09:00:00Z",
            }
        ],
        "get_payment_timeline": {
            "order_id": "order-1",
            "payments": [
                {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "89.00"}
            ],
            "events": [
                {
                    "event_at": "2024-02-01T10:00:00Z",
                    "event_type": "captured",
                    "amount_brl": "89.00",
                    "status": "confirmed",
                }
            ],
        },
        "get_shipment_summary": {
            "delivered_carrier_at": "2024-02-03T00:00:00Z",
            "delivered_customer_at": "2024-02-10T00:00:00Z",
            "estimated_delivery_at": "2024-02-11T00:00:00Z",
            "events": [],
        },
        "get_policy": {"rules": rules},
        "get_refund_timeline": {"events": []},
    }
    records = {
        tool: {"data": value, "evidence_ref": f"ev_test_only_{index:020d}"}
        for index, (tool, value) in enumerate(data.items())
    }
    return case, records


def check(case, records):
    output = assess(case, records)
    Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas").validate_output(
        output, "test output"
    )
    refs = {r["evidence_ref"] for r in records.values() if "evidence_ref" in r}
    assert set(output["evidence_refs"]) <= refs
    for claim in output["claim_assessments"]:
        assert set(claim["evidence_refs"]) <= set(output["evidence_refs"])
    finance = output["financial_resolution"]
    assert finance["recommended_refund_brl"] == sum(
        r["amount_brl"] for r in finance["refund_lines"]
    )
    return output


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_refund_reads_latest_event_not_top_level_status(scenario, status):
    case, records = scenario
    records["get_refund_timeline"]["data"]["events"] = [
        {"event_at": "2024-02-14T00:00:00Z", "status": status},
        {"event_at": "2024-02-13T00:00:00Z", "status": "requested"},
    ]
    assert check(case, records)["assessment"]["primary_issue"] == f"refund_{status}"


def test_empty_refund_envelope_does_not_swallow_other_branches(scenario):
    case, records = scenario
    records["get_payment_timeline"]["data"]["events"].append(
        {"event_type": "reconciliation_mismatch", "status": "open", "amount_brl": "35.00"}
    )
    assert check(case, records)["assessment"]["primary_issue"] == "payment_mismatch"


@pytest.mark.parametrize("status", ["canceled", "unavailable"])
def test_paid_cancellation_and_unavailability(scenario, status):
    case, records = scenario
    records["get_order"]["data"]["order_status"] = status
    output = check(case, records)
    assert output["assessment"]["primary_issue"] == f"{status}_order_paid"
    if status == "unavailable":
        assert output["root_cause_analysis"]["responsible_parties"] == [
            {"party_type": "seller", "party_id": "seller-1"}
        ]


@pytest.mark.parametrize(
    "carrier,issue",
    [("2024-02-06", "late_delivery_seller"), ("2024-02-03", "late_delivery_logistics")],
)
def test_late_delivery_uses_real_datetimes_and_handoff(scenario, carrier, issue):
    case, records = scenario
    shipment = records["get_shipment_summary"]["data"]
    shipment["delivered_customer_at"] = "2024-02-13T00:00:00Z"
    shipment["delivered_carrier_at"] = carrier + "T00:00:00Z"
    output = check(case, records)
    assert output["assessment"]["primary_issue"] == issue
    assert output["claim_assessments"][0]["verdict"] == "partially_supported"


def test_late_handoff_but_on_time_delivery_is_not_late_delivery(scenario):
    case, records = scenario
    records["get_shipment_summary"]["data"]["delivered_carrier_at"] = "2024-02-06T00:00:00Z"
    assert check(case, records)["assessment"]["primary_issue"] == "unsupported_claim"


@pytest.mark.parametrize(
    "amount,issue", [("44.50", "valid_split_payment"), ("89.00", "duplicate_charge")]
)
def test_equal_payments_need_total_reconciliation(scenario, amount, issue):
    case, records = scenario
    case["customer_request"]["claims"][0]["topic"] = "duplicate_charge"
    data = records["get_payment_timeline"]["data"]
    data["payments"] = [
        {"payment_sequential": str(n), "payment_type": "credit_card", "payment_value": amount}
        for n in (1, 2)
    ]
    data["events"] = [
        {
            "event_type": "captured",
            "status": "confirmed",
            "amount_brl": amount,
            "event_at": f"2024-02-01T{hour}:00:00Z",
        }
        for hour in (10, 11)
    ]
    assert check(case, records)["assessment"]["primary_issue"] == issue


def test_claim_cannot_create_duplicate_charge(scenario):
    case, records = scenario
    case["customer_request"]["claims"][0]["topic"] = "duplicate_charge"
    output = check(case, records)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["claim_assessments"][0]["verdict"] == "unsupported"


def test_failed_refund_lookup_is_unknown_not_no_action(scenario):
    case, records = scenario
    case["customer_request"]["claims"][0]["topic"] = "refund_pending"
    records["get_refund_timeline"] = {"error": "tool_execution_error"}
    output = check(case, records)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["confidence"] < 0.6


def test_completed_refund_does_not_issue_second_refund(scenario):
    case, records = scenario
    records["get_order"]["data"]["order_status"] = "canceled"
    records["get_refund_timeline"]["data"]["events"] = [{"status": "completed"}]
    assert check(case, records)["financial_resolution"]["recommended_refund_brl"] == 0


def test_conflicting_item_versions_do_not_mix_past_or_future_events(scenario):
    case, records = scenario
    old_item = deepcopy(records["get_order_items"]["data"][0])
    old_item["shipping_limit_date"] = "2024-01-04T09:00:00Z"
    records["get_order_items"]["data"].append(old_item)
    records["get_payment_timeline"]["data"]["events"].append(
        {
            "event_type": "captured",
            "event_at": "2024-01-01T10:00:00Z",
            "amount_brl": "89.00",
            "status": "confirmed",
        }
    )
    records["get_refund_timeline"]["data"]["events"] = [
        {"event_at": "2024-01-12T00:00:00Z", "status": "failed"}
    ]
    output = check(case, records)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["data_conflicts"]


def test_cross_order_payload_is_rejected(scenario):
    case, records = scenario
    records["get_order"]["data"]["order_id"] = "another-order"
    with pytest.raises(ValueError, match="different order"):
        assess(case, records)


def test_timezone_comparisons_are_chronological():
    assert timestamp("2024-01-01T10:00:00-03:00") > timestamp("2024-01-01T12:00:00Z")


def add_later_version(records):
    item = deepcopy(records["get_order_items"]["data"][0])
    item["shipping_limit_date"] = "2024-02-12T09:00:00Z"
    records["get_order_items"]["data"].append(item)
    records["get_payment_timeline"]["data"]["events"].append(
        {
            "event_at": "2024-02-09T10:00:00Z",
            "event_type": "captured",
            "amount_brl": "35.00",
            "status": "confirmed",
        }
    )


def test_purchase_anchor_beats_later_capture_even_when_claim_is_wrong(scenario):
    case, records = scenario
    add_later_version(records)
    records["get_order"]["data"]["order_status"] = "canceled"
    case["customer_request"]["claims"][0]["topic"] = "late_delivery_seller"
    records["get_shipment_summary"]["data"]["events"] = [
        {
            "event_at": "2024-02-22T00:00:00Z",
            "event_type": "delivered_late",
            "actor": "seller",
            "status": "confirmed",
        }
    ]
    output = check(case, records)
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["claim_assessments"][0]["verdict"] == "unsupported"


def test_refund_after_next_capture_retains_unique_transaction_amount(scenario):
    case, records = scenario
    add_later_version(records)
    records["get_refund_timeline"]["data"]["events"] = [
        {
            "event_at": "2024-02-14T00:00:00Z",
            "status": "pending",
            "amount_brl": "89.00",
        }
    ]
    assert check(case, records)["assessment"]["primary_issue"] == "refund_pending"


def test_mismatching_refund_amount_does_not_attach_to_earlier_purchase(scenario):
    case, records = scenario
    add_later_version(records)
    records["get_refund_timeline"]["data"]["events"] = [
        {
            "event_at": "2024-02-14T00:00:00Z",
            "status": "failed",
            "amount_brl": "35.00",
        }
    ]
    assert check(case, records)["assessment"]["primary_issue"] == "unsupported_claim"


def test_newer_agreeing_delivery_snapshots_supersede_older_late_event(scenario):
    case, records = scenario
    records["get_order"]["data"]["order_delivered_customer_date"] = "2024-02-10T00:00:00Z"
    records["get_shipment_summary"]["data"]["events"] = [
        {
            "event_at": "2024-02-07T00:00:00Z",
            "event_type": "delivered_late",
            "actor": "logistics_provider",
            "status": "confirmed",
        }
    ]
    output = check(case, records)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert any(
        c["resolution_code"] == "SELECT_NEWER_CONFIRMED_DELIVERY" for c in output["data_conflicts"]
    )


def test_conflicting_snapshots_do_not_silently_discard_late_event(scenario):
    case, records = scenario
    records["get_order"]["data"]["order_delivered_customer_date"] = "2024-02-09T00:00:00Z"
    records["get_shipment_summary"]["data"]["events"] = [
        {
            "event_at": "2024-02-07T00:00:00Z",
            "event_type": "delivered_late",
            "actor": "logistics_provider",
            "status": "confirmed",
        }
    ]
    assert check(case, records)["assessment"]["primary_issue"] == "late_delivery_logistics"


def test_completed_refund_evidence_is_cited_for_no_action(scenario):
    case, records = scenario
    records["get_order"]["data"]["order_status"] = "canceled"
    records["get_refund_timeline"]["data"]["events"] = [{"status": "completed"}]
    output = check(case, records)
    assert records["get_refund_timeline"]["evidence_ref"] in output["evidence_refs"]


def test_resolved_temporal_conflicts_differ_from_source_warnings(scenario):
    case, records = scenario
    add_later_version(records)
    records["get_order"]["data"]["order_status"] = "canceled"
    assert check(case, records)["assessment"]["confidence"] == 0.92
    records["get_payment_timeline"]["warnings"] = ["Incomplete payment history"]
    assert check(case, records)["assessment"]["confidence"] <= 0.8
