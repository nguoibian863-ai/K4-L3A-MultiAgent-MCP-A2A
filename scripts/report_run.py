"""Compare local artifacts and check evidence capture; never estimate private scores."""

import argparse
import json
from collections import Counter
from pathlib import Path

from student_agent.cases import load_case_set
from student_agent.contracts import Contracts
from student_agent.submission import validate_artifacts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("--baseline", type=Path, default=Path("outputs"))
    args = parser.parse_args()
    outputs, trace = validate_artifacts(
        args.artifacts, load_case_set(Path.cwd()), Contracts(Path("contracts/schemas"))
    )
    captured = {}
    failures = Counter()
    calls = Counter()
    for line in (args.artifacts / "evidence.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        calls[row["tool"]] += 1
        evidence = row["response"]
        if "evidence_ref" in evidence:
            captured[evidence["evidence_ref"]] = row["case_id"]
        else:
            failures[row["tool"]] += 1
    baseline = {
        p.stem: json.loads(p.read_text(encoding="utf-8")) for p in args.baseline.glob("*.json")
    }
    changes = []
    for case_id, output in outputs.items():
        for ref in output["evidence_refs"]:
            if captured.get(ref) != case_id:
                raise ValueError(f"{case_id}: ref absent from same-case capture")
        old = baseline.get(case_id, {}).get("assessment", {}).get("primary_issue")
        new = output["assessment"]["primary_issue"]
        if old != new:
            changes.append({"case_id": case_id, "before": old, "after": new})
    counts = Counter(o["assessment"]["primary_issue"] for o in outputs.values())
    old_counts = Counter(o["assessment"]["primary_issue"] for o in baseline.values())
    summary = {
        "case_count": len(outputs),
        "trace_events": len(trace),
        "changed_primary_issues": len(changes),
        "recorded_calls": dict(calls),
        "recorded_tool_errors": dict(failures),
        "issue_counts": dict(counts),
        "changes": changes,
        "official_score": None,
    }
    (args.artifacts / "report.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    lines = [
        "# Báo cáo kiểm tra bản cải tiến",
        "",
        f"- Output đúng schema và kiểm tra nhất quán: {len(outputs)}/100.",
        f"- Số trace event: {len(trace)}.",
        "- Mọi evidence ref được trích dẫn có trong capture MCP của đúng case.",
        f"- Số case thay đổi primary issue so với bản cũ: {len(changes)}.",
        "- Chưa có điểm chấm chính thức; thay đổi kết luận không đồng nghĩa với tăng accuracy.",
        "",
        "| Primary issue | Bản cũ | Bản mới |",
        "| --- | ---: | ---: |",
    ]
    for issue in sorted(set(counts) | set(old_counts)):
        lines.append(f"| {issue} | {old_counts[issue]} | {counts[issue]} |")
    lines += ["", "Lỗi tool được ghi nhận (không tính lỗi transport chưa nhận được phản hồi):"]
    lines += [f"- `{tool}`: {count}" for tool, count in sorted(failures.items())]
    lines += [
        "",
        "Confidence là heuristic theo chất lượng bằng chứng; chưa fit trên ground truth.",
        "Artifact cục bộ không thay thế kiểm tra team/run/audit ở máy chủ competition.",
    ]
    (args.artifacts / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "changes"}, indent=2))


if __name__ == "__main__":
    main()
