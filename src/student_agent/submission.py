from __future__ import annotations

import json
import re
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import OUTPUT_SCHEMA_VERSION, VARIANT_ID
from .cases import CaseSet
from .contracts import Contracts

SECRET_PATTERN = re.compile(r"sk-team-[A-Za-z0-9_-]{8,}")
MAX_FILE_BYTES = 1024 * 1024
MAX_SUBMISSION_BYTES = 12 * 1024 * 1024


def validate_case_consistency(output: dict[str, Any], events: list[dict[str, Any]]) -> None:
    """Check public invariants without pretending to reproduce the private scorer."""
    case_id = output["case_id"]
    finance = output["financial_resolution"]
    amount = Decimal(str(finance["recommended_refund_brl"]))
    total = sum((Decimal(str(r["amount_brl"])) for r in finance["refund_lines"]), Decimal(0))
    if amount != total:
        raise ValueError(f"{case_id}: refund total does not equal refund lines")
    status = output["assessment"]["case_status"]
    if status != "action_required" and (amount or finance["refund_lines"]):
        raise ValueError(f"{case_id}: non-actionable case has a refund")
    sellers = set(output["affected_entities"]["seller_ids"])
    for party in output["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] == "seller" and party["party_id"] not in sellers:
            raise ValueError(f"{case_id}: responsible seller is outside affected sellers")
    types = [event["event_type"] for event in events]
    required = {
        "case_received",
        "task_assigned",
        "handoff",
        "verification_completed",
        "case_finalized",
    }
    if not required <= set(types):
        raise ValueError(f"{case_id}: missing workflow events")
    if (
        types[0] != "case_received"
        or types[-1] != "case_finalized"
        or types.count("case_received") != 1
        or types.count("case_finalized") != 1
    ):
        raise ValueError(f"{case_id}: invalid receive/finalize ordering")
    if types.index("verification_completed") > types.index("case_finalized"):
        raise ValueError(f"{case_id}: finalized before verification")
    consumed = {
        ref
        for event in events
        if event["event_type"] == "tool_result_consumed"
        for ref in event.get("evidence_refs", [])
    }
    refs = set(output["evidence_refs"])
    if not refs or not refs <= consumed:
        raise ValueError(f"{case_id}: output evidence not consumed in its trace")
    for claim in output.get("claim_assessments", []):
        if not set(claim["evidence_refs"]) <= refs:
            raise ValueError(f"{case_id}: claim evidence is outside output evidence")


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def build_manifest(case_set: CaseSet) -> dict[str, Any]:
    return {
        "schema_version": "day09-submission-manifest-v2",
        "competition_id": "day09-multiagent-mcp-a2a",
        "variant_id": VARIANT_ID,
        "case_set_version": case_set.version,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "trace_schema_version": "day09-trace-event-v1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "client": {"name": "day09-student-starter", "version": "0.1.0"},
    }


def validate_artifacts(
    root: Path, case_set: CaseSet, contracts: Contracts
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    outputs_root = root / "outputs"
    actual = {path.stem: path for path in outputs_root.glob("*.json") if path.is_file()}
    expected = set(case_set.case_ids)
    if set(actual) != expected:
        missing = sorted(expected - set(actual))
        extra = sorted(set(actual) - expected)
        raise ValueError(f"outputs do not match case-set; missing={missing}, extra={extra}")

    outputs: dict[str, dict[str, Any]] = {}
    for case_id in case_set.case_ids:
        output = _json_object(actual[case_id])
        contracts.validate_output(output, f"outputs/{case_id}.json")
        if output.get("case_id") != case_id:
            raise ValueError(f"outputs/{case_id}.json has a mismatched case_id")
        outputs[case_id] = output

    trace_path = root / "traces" / "trace.jsonl"
    try:
        trace_lines = trace_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("traces/trace.jsonl is missing or not UTF-8") from exc
    normalized_lines: list[str] = []
    seen_events: set[str] = set()
    events_by_case: dict[str, list[dict[str, Any]]] = {case_id: [] for case_id in expected}
    ref_owners: dict[str, str] = {}
    for number, line in enumerate(trace_lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"traces/trace.jsonl:{number}: invalid JSON") from exc
        contracts.validate_trace(event, f"traces/trace.jsonl:{number}")
        if event["case_id"] not in expected:
            raise ValueError(f"traces/trace.jsonl:{number}: case is outside this case-set")
        if event["event_id"] in seen_events:
            raise ValueError(f"traces/trace.jsonl:{number}: duplicate event_id")
        seen_events.add(event["event_id"])
        events_by_case[event["case_id"]].append(event)
        for ref in event.get("evidence_refs", []):
            owner = ref_owners.setdefault(ref, event["case_id"])
            if owner != event["case_id"]:
                raise ValueError("evidence reference is shared across cases")
        normalized_lines.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))

    serialized = [json.dumps(value, ensure_ascii=False) for value in outputs.values()]
    if SECRET_PATTERN.search("\n".join([*serialized, *normalized_lines])):
        raise ValueError("a Team API Key appears in output or trace")
    for case_id, output in outputs.items():
        validate_case_consistency(output, events_by_case[case_id])
    return outputs, normalized_lines


def package_submission(root: Path, destination: Path, artifacts_root: Path | None = None) -> Path:
    from .cases import load_case_set

    root = root.resolve()
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    outputs, trace_lines = validate_artifacts(artifacts_root or root, case_set, contracts)
    manifest = build_manifest(case_set)
    contracts.validate_manifest(manifest)

    payloads = {
        "manifest.json": json.dumps(manifest, separators=(",", ":")).encode(),
        "trace.jsonl": ("\n".join(trace_lines) + ("\n" if trace_lines else "")).encode(),
        **{
            f"outputs/{case_id}.json": json.dumps(
                outputs[case_id], ensure_ascii=False, separators=(",", ":")
            ).encode()
            for case_id in case_set.case_ids
        },
    }
    oversized = [name for name, payload in payloads.items() if len(payload) > MAX_FILE_BYTES]
    if oversized:
        raise ValueError(f"submission files exceed 1 MB: {oversized}")
    if sum(map(len, payloads.values())) > MAX_SUBMISSION_BYTES:
        raise ValueError("submission exceeds the 12 MB uncompressed limit")

    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    return destination
