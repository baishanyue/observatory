#!/usr/bin/env python3
"""Observatory benchmarks: trace-tree build, replay, diff, graph ordering.

Run with::

    python3 benchmarks/bench.py
    python3 benchmarks/bench.py --json

Numbers are indicative for this machine only, not comparisons with other
projects.  The workloads are synthetic but shaped like real agent traces:
a shallow trace forest, sequential step records and JSON-sized payloads.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from observatory import (  # noqa: E402
    FlowGraph,
    Run,
    Step,
    build_tree,
    diff_runs,
    flatten,
    replay_run,
)


def _run(steps: int, *, run_id: str = "run_bench") -> Run:
    records = []
    for index in range(steps):
        node_id = f"node_{index:04d}"
        parent = None
        if index % 9:
            parent = f"s{index - 1:04d}"
        records.append(
            Step(
                id=f"s{index:04d}",
                node_id=node_id,
                label=f"Step {index}",
                kind="agent" if index % 5 else "tool",
                status="ok",
                parent=parent,
                started_ms=float(index * 40),
                latency_ms=float(20 + (index * 17) % 380),
                tokens_in=80 + (index * 13) % 900,
                tokens_out=40 + (index * 7) % 400,
                output=f"step {index} produced a deterministic result",
            )
        )
    return Run(
        id=run_id,
        flow_id="bench-flow",
        flow_name="Benchmark Flow",
        started_at="2026-01-01T00:00:00Z",
        steps=records,
    )


def _graph(nodes: int = 1000) -> FlowGraph:
    graph = FlowGraph("Benchmark Graph", "bench-graph")
    for index in range(nodes):
        graph.add_node(f"n{index:04d}", "agent", f"Node {index}")
    for index in range(1, nodes):
        graph.add_edge(f"n{index - 1:04d}", f"n{index:04d}")
        if index % 11 == 0 and index > 12:
            graph.add_edge(f"n{index - 12:04d}", f"n{index:04d}")
    return graph


def _measure(
    label: str,
    unit: str,
    func: Callable[[], Any],
    *,
    iterations: int,
    repeats: int = 3,
) -> dict[str, Any]:
    func()  # warm up
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        for _ in range(iterations):
            func()
        elapsed = time.perf_counter() - start
        samples.append(elapsed / iterations)
    per_op = min(samples)
    return {
        "label": label,
        "unit": unit,
        "iterations": iterations,
        "per_op_ms": per_op * 1000,
        "per_op_us": per_op * 1_000_000,
        "throughput": 1.0 / per_op,
        "samples": len(samples),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Observatory benchmarks")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)

    graph = _graph(1000)
    tree_run = _run(512, run_id="run_tree")
    replay_run_source = _run(256, run_id="run_replay")
    diff_left = _run(256, run_id="run_left")
    diff_right = _run(256, run_id="run_right")

    results = [
        _measure(
            "graph topological order (1,000 nodes)",
            "orders/s",
            graph.topological_order,
            iterations=60,
        ),
        _measure(
            "trace tree build + flatten (512 steps)",
            "builds/s",
            lambda: flatten(build_tree(tree_run)),
            iterations=400,
        ),
        _measure(
            "replay (256 steps)",
            "replays/s",
            lambda: replay_run(replay_run_source, replay_id="run_bench_replay"),
            iterations=600,
        ),
        _measure(
            "structural diff (256 steps x 2)",
            "diffs/s",
            lambda: diff_runs(diff_left, diff_right),
            iterations=300,
        ),
        _measure(
            "run JSON serialisation (256 steps)",
            "serialisations/s",
            tree_run.to_json,
            iterations=300,
        ),
    ]

    environment = {
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine() or "unknown",
        "processor": platform.processor() or "unknown",
        "cpus": os.cpu_count(),
    }

    if args.json:
        print(
            json.dumps(
                {"environment": environment, "results": results},
                indent=2,
            )
        )
        return 0

    print("Observatory benchmarks")
    print(
        f"{environment['timestamp']}  ·  Python {environment['python']}  ·  "
        f"{environment['platform']}  ·  {environment['machine']}  ·  "
        f"{environment['cpus']} cpus"
    )
    print()
    print("| Workload | Iterations | Per operation | Throughput |")
    print("| --- | ---: | ---: | ---: |")
    for item in results:
        if item["per_op_ms"] >= 1:
            per_op = f"{item['per_op_ms']:.2f} ms"
        else:
            per_op = f"{item['per_op_us']:.1f} µs"
        print(
            f"| {item['label']} | {item['iterations']} | {per_op} | "
            f"{item['throughput']:,.0f} {item['unit']} |"
        )
    print()
    print(
        "Best of 3 timed samples after one warm-up. Synthetic single-process "
        "workloads; indicative, not comparative claims."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
