"""Observatory — a visual workbench for agent and RAG runs."""

from .graph import KINDS, Edge, FlowGraph, GraphError, Node
from .inspector import (
    TraceNode,
    build_tree,
    flatten,
    format_ms,
    metrics as trace_metrics,
    summarize,
    tree_dict,
)
from .runs import (
    Run,
    RunStore,
    Step,
    diff_runs,
    format_diff,
    new_run_id,
    replay_run,
)

__version__ = "0.2.0"

__all__ = [
    "__version__",
    "KINDS",
    "Node",
    "Edge",
    "FlowGraph",
    "GraphError",
    "Step",
    "Run",
    "RunStore",
    "new_run_id",
    "replay_run",
    "diff_runs",
    "format_diff",
    "TraceNode",
    "build_tree",
    "flatten",
    "tree_dict",
    "summarize",
    "trace_metrics",
    "format_ms",
]
