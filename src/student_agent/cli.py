from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx2
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT

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


async def _run(root: Path, artifacts: Path | None = None) -> Path:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    if artifacts is None:
        artifacts = root / "dist" / "runs" / f"{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
    # Each execution gets its own artifact directory; never mix refs from different runs.
    if (artifacts / "traces" / "trace.jsonl").exists() or list(
        (artifacts / "outputs").glob("*.json")
    ):
        raise ValueError("Artifact directory is not empty; choose a new --artifacts-dir")
    output_root = artifacts / "outputs"
    trace_path = artifacts / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    async with httpx2.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{settings.competition_api_url}/api/v2/runs",
            headers={"Authorization": f"Bearer {settings.team_api_key}"},
            json={"variant_id": "l3a"},
        )
        response.raise_for_status()
        result = response.json()
        metadata = {
            "created_at": datetime.now(UTC).isoformat(),
            "variant_id": "l3a",
            "case_set_version": result.get("case_set_version"),
            "expires_at": result.get("expires_at"),
        }
        if result.get("variant_id") != "l3a":
            raise ValueError("Run API returned an unexpected variant")
        if result.get("case_set_version") != case_set.version:
            raise ValueError("Run API case-set version differs from local inputs")
        (artifacts / "run.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    semaphore = asyncio.Semaphore(2)

    def transient(exc: BaseException) -> bool:
        if isinstance(exc, BaseExceptionGroup):
            return all(transient(child) for child in exc.exceptions)
        if isinstance(exc, MCPError):
            return exc.code in {CONNECTION_CLOSED, REQUEST_TIMEOUT}
        return isinstance(exc, (httpx2.TransportError, TimeoutError, ConnectionError))

    async def run_case(case_id: str) -> None:
        async with semaphore:
            case = case_set.cases[case_id]
            case_cache: dict[tuple, dict] = {}
            case_trace_path = artifacts / "case-traces" / f"{case_id}.jsonl"
            for attempt in range(3):
                case_trace_path.parent.mkdir(parents=True, exist_ok=True)
                case_trace_path.write_text("", encoding="utf-8")
                trace = TraceWriter(case_trace_path, contracts)
                trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                try:
                    async with connect_gateway(
                        settings.mcp_endpoint,
                        settings.team_api_key,
                        contracts,
                        artifacts / "evidence.jsonl",
                        case_cache,
                    ) as gateway:
                        if not await gateway.list_tools():
                            raise RuntimeError("MCP Gateway returned no tools")
                        output = await solve_case(case, gateway, trace)
                    break
                except Exception as exc:
                    if not transient(exc) or attempt == 2:
                        raise
                    print(f"{case_id}: reconnecting after transport failure", flush=True)
                    await asyncio.sleep(1 + attempt)
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
            print(
                f"{case_id}: {output['assessment']['primary_issue']} "
                f"({output['assessment']['confidence']:.2f})",
                flush=True,
            )

    await asyncio.gather(*(run_case(case_id) for case_id in case_set.case_ids))
    trace_path.write_text(
        "".join(
            (artifacts / "case-traces" / f"{case_id}.jsonl").read_text(encoding="utf-8")
            for case_id in case_set.case_ids
        ),
        encoding="utf-8",
    )
    validate_artifacts(artifacts, case_set, contracts)
    # Sync to root outputs and traces for standard grading tools
    root_outputs = root / "outputs"
    root_traces = root / "traces" / "trace.jsonl"
    root_outputs.mkdir(parents=True, exist_ok=True)
    root_traces.parent.mkdir(parents=True, exist_ok=True)
    for f in output_root.glob("*.json"):
        (root_outputs / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    root_traces.write_text(trace_path.read_text(encoding="utf-8"), encoding="utf-8")

    print(f"Artifacts: {artifacts}")
    return artifacts


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument("--artifacts-dir", help="new directory (default: dist/runs/<timestamp>)")
    check = commands.add_parser("validate", help="validate outputs and observable trace")
    check.add_argument("--artifacts-dir", default=".")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    package.add_argument("--artifacts-dir", default=".")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            target = args.artifacts_dir or f"dist/runs/{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
            asyncio.run(_run(root, root / target))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root / args.artifacts_dir, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            package_submission(root, root / args.output, root / args.artifacts_dir)
            print(f"OK: {args.output}")
    except (OSError, RuntimeError, ValueError, httpx2.HTTPError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
