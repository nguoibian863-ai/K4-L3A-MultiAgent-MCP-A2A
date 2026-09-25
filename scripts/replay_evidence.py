"""Local evidence replay for debugging; does not create a submission or claim a score."""

import argparse
import json
from collections import Counter
from pathlib import Path

from student_agent.analysis import assess
from student_agent.cases import load_case_set
from student_agent.contracts import Contracts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    cases = load_case_set(Path.cwd())
    contracts = Contracts(Path("contracts/schemas"))
    records = {}
    if args.source.is_dir():
        records = {
            p.stem: json.loads(p.read_text(encoding="utf-8")) for p in args.source.glob("*.json")
        }
    else:
        for line in args.source.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            records.setdefault(row["case_id"], {})[row["tool"]] = row["response"]
    counts = Counter()
    for case_id, evidence in sorted(records.items()):
        output = assess(cases.cases[case_id], evidence)
        contracts.validate_output(output, case_id)
        issue = output["assessment"]["primary_issue"]
        counts[issue] += 1
        print(case_id, issue, output["assessment"]["confidence"])
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
