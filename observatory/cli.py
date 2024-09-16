"""Command line entry point for Observatory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .graph import FlowGraph, GraphError
from .inspector import summarize
from .runs import Run, RunError, StoreError
from .server import DEFAULT_EXAMPLES, load_example_flows, seed_studio, serve

DESCRIPTION = "Observatory — compose, inspect, and replay agent runs."


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="observatory",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  observatory validate workspace/flows/research-agent.json\n"
            "  observatory summary workspace/runs/run_7fc21a9d04.json\n"
            "  observatory serve --port 8787"
        ),
    )
    parser.add_argument("--version", action="version", version="observatory 0.2.0")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    validate = sub.add_parser("validate", help="check a flow graph for structural errors")
    validate.add_argument("path", help="path to a flow JSON file")
    validate.add_argument("--json", action="store_true", help="print a JSON report")
    validate.set_defaults(func=_cmd_validate)

    summary = sub.add_parser("summary", help="summarise a run, store, or flow")
    summary.add_argument("path", help="path to a run, run store, or flow JSON file")
    summary.add_argument(
        "--steps", type=int, default=32, help="maximum trace rows to print (default 32)"
    )
    summary.add_argument(
        "--outputs", action="store_true", help="include step output previews"
    )
    summary.add_argument("--json", action="store_true", help="print a JSON report")
    summary.set_defaults(func=_cmd_summary)

    serve_cmd = sub.add_parser("serve", help="start the local workbench server")
    serve_cmd.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    serve_cmd.add_argument("--port", type=int, default=8787, help="bind port (default 8787)")
    serve_cmd.add_argument(
        "--examples",
        default=None,
        help=f"flow directory or file (default {DEFAULT_EXAMPLES})",
    )
    serve_cmd.add_argument(
        "--data", default=None, help="run store JSON path (default .observatory/runs.json)"
    )
    serve_cmd.add_argument(
        "--memory", action="store_true", help="keep runs in memory, do not persist"
    )
    serve_cmd.add_argument("--verbose", action="store_true", help="log HTTP requests")
    serve_cmd.set_defaults(func=_cmd_serve)
    return parser


def _load(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise GraphError(f"cannot read {path}: {exc.strerror or exc}") from None
    except json.JSONDecodeError as exc:
        raise GraphError(f"invalid JSON in {path}: {exc}") from None


def _cmd_validate(args: argparse.Namespace) -> int:
    data = _load(args.path)
    if not isinstance(data, dict) or "nodes" not in data:
        raise GraphError("validate expects a flow JSON object with 'nodes' and 'edges'")
    graph = FlowGraph.from_dict(data)
    report = graph.summary()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["valid"] else 1
    print(f"{graph.name} / {graph.id}")
    print(f"nodes {graph.node_count} / edges {graph.edge_count}")
    if report["valid"]:
        print("order " + " -> ".join(report["order"]))
        print("valid")
        return 0
    print("invalid")
    for issue in report["issues"]:
        print(f"  - {issue}")
    return 1


def _cmd_summary(args: argparse.Namespace) -> int:
    data = _load(args.path)
    if not isinstance(data, dict):
        raise GraphError("summary expects a JSON object")

    if "runs" in data:
        store_runs = [Run.from_dict(item) for item in data["runs"]]
        if not store_runs:
            print("empty run store")
            return 0
        if args.json:
            print(
                json.dumps(
                    [run.metrics() | {"id": run.id, "flow": run.flow_name} for run in store_runs],
                    indent=2,
                )
            )
            return 0
        for index, run in enumerate(store_runs):
            if index:
                print()
            print(summarize(run, max_steps=args.steps, show_outputs=args.outputs))
        return 0

    if "steps" in data:
        run = Run.from_dict(data)
        if args.json:
            print(json.dumps(run.metrics() | {"id": run.id, "flow": run.flow_name}, indent=2))
        else:
            print(summarize(run, max_steps=args.steps, show_outputs=args.outputs))
        return 0

    if "nodes" in data:
        graph = FlowGraph.from_dict(data)
        report = graph.summary()
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report["valid"] else 1
        print(f"{graph.name} / {graph.id}")
        print(f"nodes {graph.node_count} / edges {graph.edge_count}")
        if report["valid"]:
            print("order " + " -> ".join(report["order"]))
            print("valid")
            return 0
        print("invalid")
        for issue in report["issues"]:
            print(f"  - {issue}")
        return 1

    raise GraphError("unrecognised JSON: expected a flow, run, or run store")


def _cmd_serve(args: argparse.Namespace) -> int:
    if args.memory:
        from .server import Studio

        studio = Studio(flows=load_example_flows(args.examples).values())
    else:
        studio = seed_studio(examples=args.examples, data=args.data)
    serve(
        studio,
        host=args.host,
        port=args.port,
        verbose=args.verbose,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except (GraphError, RunError, StoreError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
