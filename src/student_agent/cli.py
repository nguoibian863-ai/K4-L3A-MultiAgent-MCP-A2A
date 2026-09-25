from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _run(root: Path, target_case_id: str | None = None, force: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    if force and not target_case_id:
        for existing_file in output_root.glob("*.json"):
            existing_file.unlink(missing_ok=True)
        if trace_path.exists():
            trace_path.unlink(missing_ok=True)

    trace = TraceWriter(trace_path, contracts)

    if target_case_id:
        if target_case_id not in case_set.cases:
            raise ValueError(f"Unknown case_id: {target_case_id}")
        case = case_set.cases[target_case_id]
        print(f"Running single case: {target_case_id}...")
        async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
            trace.emit(case_id=target_case_id, event_type="case_received", actor="coordinator")
            output = await solve_case(case, gateway, trace)
            contracts.validate_output(output, f"outputs/{target_case_id}.json")
            target = output_root / f"{target_case_id}.json"
            target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            trace.emit(case_id=target_case_id, event_type="case_finalized", actor="coordinator")
            print(f"\n=== KẾT QUẢ {target_case_id} ===")
            print(json.dumps(output, ensure_ascii=False, indent=2))
            print(f"\n[OK] Output hợp lệ JSON Schema và đã lưu tại: {target}")
        return

    remaining_case_ids = []
    for case_id in case_set.case_ids:
        target = output_root / f"{case_id}.json"
        if target.is_file():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
                contracts.validate_output(data, f"outputs/{case_id}.json")
                continue
            except Exception:
                target.unlink(missing_ok=True)
        remaining_case_ids.append(case_id)

    if not remaining_case_ids:
        print(f"All {len(case_set.case_ids)} cases already completed!")
        return

    print(f"Processing {len(remaining_case_ids)} / {len(case_set.case_ids)} remaining cases...")

    case_idx = len(case_set.case_ids) - len(remaining_case_ids)
    while remaining_case_ids:
        try:
            async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
                while remaining_case_ids:
                    case_id = remaining_case_ids[0]
                    case = case_set.cases[case_id]
                    trace.start_transaction()
                    try:
                        trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                        output = await solve_case(case, gateway, trace)
                        contracts.validate_output(output, f"outputs/{case_id}.json")
                        if output.get("case_id") != case_id:
                            raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                        target = output_root / f"{case_id}.json"
                        temporary = target.with_suffix(".json.tmp")
                        temporary.write_text(
                            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                        )
                        temporary.replace(target)
                        trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
                        trace.commit_transaction()
                    except Exception:
                        trace.rollback_transaction()
                        raise
                    case_idx += 1
                    print(f"[{case_idx}/100] Case {case_id} completed: {output['assessment']['primary_issue']}")
                    remaining_case_ids.pop(0)
        except Exception as exc:
            print(f"Connection dropped ({exc}), reconnecting in 2s...")
            await asyncio.sleep(2.0)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run_parser = commands.add_parser("run", help="run the implemented workflow for all cases")
    run_parser.add_argument("--case", dest="case_id", default=None, help="run a single case ID (e.g. L3A_CASE_001)")
    run_parser.add_argument("--force", action="store_true", help="force rerun all cases, clearing previous outputs and trace")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, target_case_id=args.case_id, force=getattr(args, "force", False)))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {args.output}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
