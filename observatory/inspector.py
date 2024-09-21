"""Trace inspection: tree building, flattening and compact summaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from .runs import Run, Step

__all__ = [
    "TraceNode",
    "build_tree",
    "flatten",
    "tree_dict",
    "metrics",
    "summarize",
    "format_ms",
]


@dataclass(slots=True)
class TraceNode:
    """A step plus its nested child steps."""

    step: Step
    children: list["TraceNode"] = field(default_factory=list)
    depth: int = 0

    def walk(self) -> Iterator["TraceNode"]:
        yield self
        for child in self.children:
            yield from child.walk()


def build_tree(run: Run) -> list[TraceNode]:
    """Nest steps by ``parent`` reference.

    A parent may name a step id or a node id (the first step of that node).  A
    missing or cyclic parent reference falls back to a root so that a damaged
    trace is still inspectable.
    """
    nodes = {step.id: TraceNode(step=step) for step in run.steps}
    first_by_node: dict[str, TraceNode] = {}
    for step in run.steps:
        first_by_node.setdefault(step.node_id, nodes[step.id])

    roots: list[TraceNode] = []
    parent_of: dict[str, str] = {}
    for step in run.steps:
        current = nodes[step.id]
        if not step.parent:
            roots.append(current)
            continue
        parent = nodes.get(step.parent) or first_by_node.get(step.parent)
        if parent is None or parent is current:
            roots.append(current)
            continue
        # Attaching current under parent must not close a parent cycle.
        ancestor = parent
        cyclic = False
        guard = 0
        while ancestor is not None and guard <= len(nodes):
            if ancestor.step.id == current.step.id:
                cyclic = True
                break
            ancestor = nodes.get(parent_of.get(ancestor.step.id, ""))
            guard += 1
        if cyclic:
            roots.append(current)
            continue
        parent.children.append(current)
        parent_of[current.step.id] = parent.step.id

    stack = [(root, 0) for root in roots]
    while stack:
        node, depth = stack.pop()
        node.depth = depth
        stack.extend((child, depth + 1) for child in node.children)
    return roots


def flatten(roots: list[TraceNode]) -> list[TraceNode]:
    """Pre-order traversal of a trace forest."""
    out: list[TraceNode] = []
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(reversed(node.children))
    return out


def tree_dict(roots: list[TraceNode]) -> list[dict[str, Any]]:
    def encode(node: TraceNode) -> dict[str, Any]:
        return {
            "step": node.step.to_dict(),
            "depth": node.depth,
            "children": [encode(child) for child in node.children],
        }

    return [encode(root) for root in roots]


def format_ms(value: float) -> str:
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{value:.0f}ms"


def _format_tokens(value: int) -> str:
    return f"{value:,}"


def metrics(run: Run) -> dict[str, Any]:
    counts = run.status_counts()
    return {
        "run": run.id,
        "flow": run.flow_name,
        "status": run.status,
        "steps": run.step_count,
        "duration_ms": run.duration_ms,
        "latency_ms": run.total_latency_ms,
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "tokens": run.total_tokens,
        "ok": counts.get("ok", 0),
        "error": counts.get("error", 0),
        "skipped": counts.get("skipped", 0),
        "root_steps": len(build_tree(run)),
    }


def summarize(run: Run, *, max_steps: int = 32, show_outputs: bool = False) -> str:
    """Produce a compact, monospace-friendly trace summary."""
    stats = metrics(run)
    lines = [
        f"{stats['flow']} / {stats['run']} / {stats['status']}",
        (
            f"{stats['steps']} steps / {format_ms(stats['duration_ms'])} / "
            f"{_format_tokens(stats['tokens'])} tok "
            f"({_format_tokens(stats['tokens_in'])} in / "
            f"{_format_tokens(stats['tokens_out'])} out) / "
            f"ok {stats['ok']} / error {stats['error']} / skipped {stats['skipped']}"
        ),
    ]
    roots = build_tree(run)
    budget = [max(0, max_steps)]
    omitted = [0]

    def walk(nodes: list[TraceNode], prefix: str) -> None:
        for index, node in enumerate(nodes):
            last = index == len(nodes) - 1
            connector = "└─ " if last else "├─ "
            if budget[0] > 0:
                step = node.step
                label = step.label[:24].ljust(24)
                kind = step.kind[:9].ljust(9)
                line = (
                    f"{prefix}{connector}{label} {kind} "
                    f"{step.status:<7} {format_ms(step.latency_ms):>7} "
                    f"{_format_tokens(step.tokens):>6} tok"
                )
                lines.append(line.rstrip())
                if show_outputs and step.output:
                    lines.append(f"{prefix}{'   ' if last else '│  '}   {step.output[:96]}")
                budget[0] -= 1
            else:
                omitted[0] += 1
            walk(node.children, prefix + ("   " if last else "│  "))

    walk(roots, "")
    if omitted[0]:
        lines.append(f"… {omitted[0]} more steps")
    return "\n".join(lines)
