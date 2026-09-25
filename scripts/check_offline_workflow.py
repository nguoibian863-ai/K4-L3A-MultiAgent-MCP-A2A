"""Exercise the complete workflow using saved MCP evidence, with no network access."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from collections import Counter
from pathlib import Path

from student_agent.analysis import assess
from student_agent.cases import load_case_set
from student_agent.contracts import Contracts
from student_agent.submission import validate_case_consistency
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


async def check(source: Path, report: Path) -> None:
    cases = load_case_set(Path.cwd())
    contracts = Contracts(Path("contracts/schemas"))
    captured: dict[str, dict] = {}
    original_calls = Counter()
    for line in (source / "evidence.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        captured.setdefault(row["case_id"], {})[row["tool"]] = row
        original_calls[row["tool"]] += 1
    planned_calls = Counter()
    changes = []
    report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=report.parent) as temporary:
        for case_id in cases.case_ids:
            records = captured[case_id]

            class Playback:
                def __init__(self, evidence):
                    self.evidence = evidence

                async def call(self, tool, *, case_id, **arguments):
                    row = self.evidence[tool]  # Missing lookups fail; never fabricate evidence.
                    assert row["case_id"] == case_id and row["arguments"] == arguments
                    planned_calls[tool] += 1
                    if "error" in row["response"]:
                        raise RuntimeError("Recorded tool error")
                    return row["response"]

            writer = TraceWriter(Path(temporary) / f"{case_id}.jsonl", contracts)
            writer.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            output = await solve_case(cases.cases[case_id], Playback(records), writer)
            writer.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
            contracts.validate_output(output, case_id)
            events = [json.loads(line) for line in writer.path.read_text().splitlines()]
            validate_case_consistency(output, events)
            full_evidence = assess(
                cases.cases[case_id], {tool: row["response"] for tool, row in records.items()}
            )
            # Demand-driven retrieval must preserve all decisions from the full evidence.
            for field in ("assessment", "financial_resolution", "root_cause_analysis"):
                assert output[field] == full_evidence[field], (case_id, field)
            old = json.loads((source / "outputs" / f"{case_id}.json").read_text())
            if output["assessment"]["primary_issue"] != old["assessment"]["primary_issue"]:
                changes.append(
                    {
                        "case_id": case_id,
                        "before": old["assessment"]["primary_issue"],
                        "after": output["assessment"]["primary_issue"],
                    }
                )
    summary = {
        "validated_cases": len(cases.case_ids),
        "network_calls": 0,
        "baseline_recorded_calls": sum(original_calls.values()),
        "planned_calls_on_saved_evidence": sum(planned_calls.values()),
        "planned_calls_by_tool": dict(planned_calls),
        "issue_changes": changes,
        "official_score": None,
        "note": "Offline replay checks consistency, not correctness against hidden labels.",
    }
    report.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--report", type=Path, default=Path("dist/offline-check.json"))
    args = parser.parse_args()
    asyncio.run(check(args.source, args.report))
