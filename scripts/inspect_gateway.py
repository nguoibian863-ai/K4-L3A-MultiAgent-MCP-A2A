"""Inspect discovered tool contracts; never print credentials."""

import asyncio
import json
import sys

from student_agent.cases import load_case_set
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway


async def main():
    settings = Settings.load()
    contracts = Contracts(settings.root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        response = await gateway._session.list_tools()
        if len(sys.argv) == 1:
            for tool in response.tools:
                print(json.dumps(tool.model_dump(), ensure_ascii=True))
            return
        cases = load_case_set(settings.root)
        destination = settings.root / "dist" / "diagnostics"
        destination.mkdir(parents=True, exist_ok=True)
        seen = set()
        for case in cases.cases.values():
            topics = tuple(c["topic"] for c in case["customer_request"]["claims"])
            if topics in seen:
                continue
            seen.add(topics)
            records = {}
            for tool in response.tools:
                if tool.name in {"get_customer_history", "get_product_context"}:
                    continue
                args = (
                    {"policy_version": case["policy_version"]}
                    if tool.name == "get_policy"
                    else {"order_id": case["customer_request"]["claimed_order_id"]}
                )
                try:
                    records[tool.name] = await gateway.call(
                        tool.name, case_id=case["case_id"], **args
                    )
                except Exception as exc:
                    records[tool.name] = {"error": str(exc)}
            (destination / f"{case['case_id']}.json").write_text(
                json.dumps(records, ensure_ascii=True, indent=2), encoding="utf-8"
            )
            print(case["case_id"], topics, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
