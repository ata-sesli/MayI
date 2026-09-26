"""Permission fixtures score decisions; their shell commands are never executed."""

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from pathlib import Path

from .core.models import AuthorizationRequest


async def evaluate(evaluator, fixtures, *, progress=None):
    counts = {"approve": 0, "hold": 0, "deny": 0}
    false_approvals = 0
    latencies = []
    outcomes = []
    for fixture in fixtures:
        if fixture["expected"] not in counts:
            raise ValueError("Invalid expected outcome")
        request = AuthorizationRequest(
            "evaluation",
            "shell",
            fixture["command"],
            fixture.get("cwd", str(Path.cwd())),
            fixture.get("reason"),
            {"command": fixture["command"]},
            {},
        )
        start = time.perf_counter()
        result = await evaluator.authorize(request)
        latencies.append((time.perf_counter() - start) * 1000)
        counts[result.decision] += 1
        false_approvals += (
            result.decision == "approve" and fixture["expected"] != "approve"
        )
        outcomes.append(
            {
                "command": fixture["command"],
                "expected": fixture["expected"],
                **result.to_dict(),
            }
        )
        if progress:
            progress(len(outcomes), len(fixtures))
    total = len(outcomes)
    if not total:
        raise ValueError("At least one permission fixture is required")
    return {
        "total": total,
        "automatic_approval_rate": counts["approve"] / total,
        "false_automatic_approvals": false_approvals,
        "false_automatic_approval_rate": false_approvals / total,
        "hold_rate": counts["hold"] / total,
        "hard_deny_rate": counts["deny"] / total,
        "median_latency_ms": statistics.median(latencies),
        "p95_latency_ms": sorted(latencies)[math.ceil(total * 0.95) - 1],
        "outcomes": outcomes,
    }


async def _run(args):
    from .cli import build_evaluator, close_evaluator
    from .config import load_config

    fixtures = json.loads(Path(args.fixtures).read_text())
    evaluator = await build_evaluator(load_config(args.config))
    try:
        result = await evaluate(
            evaluator,
            fixtures,
            progress=lambda n, total: print(f"Evaluated {n}/{total}", file=sys.stderr),
        )
        print(json.dumps(result, indent=2))
    finally:
        await close_evaluator(evaluator)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--fixtures", default="tests/fixtures/permissions.json")
    asyncio.run(_run(parser.parse_args()))
