"""Directed flow graphs for Observatory.

A :class:`FlowGraph` is a small, JSON-serialisable DAG of workbench nodes and
directed edges.  Validation is intentionally separate from mutation: callers
can assemble a graph incrementally and ask for a full diagnosis afterwards.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

__all__ = ["KINDS", "GraphError", "Node", "Edge", "FlowGraph"]

#: Node kinds understood by the canvas and the simulator.  Unknown kinds are
#: reported by :meth:`FlowGraph.validate` rather than rejected outright so that
#: newer graphs remain loadable by older workbenches.
KINDS = frozenset(
    {"input", "retriever", "router", "tool", "agent", "judge", "memory", "output"}
)

_SLUG = re.compile(r"[^a-z0-9]+")


class GraphError(ValueError):
    """Raised when a graph cannot be built or ordered."""


@dataclass(slots=True)
class Node:
    """A single workbench node."""

    id: str
    kind: str
    label: str
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise GraphError("node id must be a non-empty string")
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise GraphError(f"node {self.id!r} needs a kind")
        if not isinstance(self.label, str) or not self.label.strip():
            self.label = self.id
        if not isinstance(self.config, Mapping):
            raise GraphError(f"node {self.id!r} config must be a mapping")
        self.config = dict(self.config)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "config": self.config,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Node":
        if not isinstance(data, Mapping):
            raise GraphError("node entry must be an object")
        try:
            return cls(
                id=data["id"],
                kind=data["kind"],
                label=data.get("label", data["id"]),
                config=data.get("config", {}),
            )
        except KeyError as exc:
            raise GraphError(f"node is missing {exc.args[0]!r}") from None


@dataclass(frozen=True, slots=True)
class Edge:
    """A directed edge between two node ids."""

    source: str
    target: str
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"source": self.source, "target": self.target}
        if self.label:
            out["label"] = self.label
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Edge":
        if not isinstance(data, Mapping):
            raise GraphError("edge entry must be an object")
        try:
            source, target = data["source"], data["target"]
        except KeyError as exc:
            raise GraphError(f"edge is missing {exc.args[0]!r}") from None
        if not isinstance(source, str) or not isinstance(target, str):
            raise GraphError("edge source and target must be strings")
        return cls(source=source, target=target, label=str(data.get("label", "")))


def _slug(text: str) -> str:
    value = _SLUG.sub("-", text.strip().lower()).strip("-")
    return value or "flow"


class FlowGraph:
    """An ordered collection of nodes and directed edges."""

    def __init__(self, name: str = "flow", flow_id: str | None = None) -> None:
        self.name = name
        self.id = flow_id or _slug(name)
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []

    # ------------------------------------------------------------- mutation
    def add_node(
        self,
        node_id: str,
        kind: str,
        label: str | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> Node:
        if node_id in self.nodes:
            raise GraphError(f"duplicate node id: {node_id}")
        node = Node(id=node_id, kind=kind, label=label or node_id, config=dict(config or {}))
        self.nodes[node.id] = node
        return node

    def add_edge(self, source: str, target: str, label: str = "") -> Edge:
        edge = Edge(source=source, target=target, label=label)
        self.edges.append(edge)
        return edge

    def remove_node(self, node_id: str) -> None:
        if node_id not in self.nodes:
            raise GraphError(f"unknown node: {node_id}")
        del self.nodes[node_id]
        self.edges = [e for e in self.edges if node_id not in (e.source, e.target)]

    # ------------------------------------------------------------ traversal
    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    def __len__(self) -> int:
        return len(self.nodes)

    def __iter__(self) -> Iterator[Node]:
        return iter(self.nodes.values())

    def node(self, node_id: str) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise GraphError(f"unknown node: {node_id}") from None

    def _outgoing(self) -> dict[str, list[str]]:
        adjacency: dict[str, list[str]] = {node_id: [] for node_id in self.nodes}
        for edge in self.edges:
            if edge.source in adjacency:
                adjacency[edge.source].append(edge.target)
        return adjacency

    def successors(self, node_id: str) -> list[str]:
        return [e.target for e in self.edges if e.source == node_id]

    def predecessors(self, node_id: str) -> list[str]:
        return [e.source for e in self.edges if e.target == node_id]

    def roots(self) -> list[str]:
        """Nodes with no incoming edges, in insertion order."""
        incoming = {e.target for e in self.edges}
        return [n for n in self.nodes if n not in incoming]

    def leaves(self) -> list[str]:
        outgoing = {e.source for e in self.edges}
        return [n for n in self.nodes if n not in outgoing]

    def orphans(self) -> list[str]:
        """Isolated nodes: no incoming and no outgoing edges."""
        connected = {e.source for e in self.edges} | {e.target for e in self.edges}
        return [n for n in self.nodes if n not in connected]

    def unreachable(self) -> list[str]:
        """Nodes not reachable from any root.

        In a DAG this is empty.  The check exists for malformed graphs where a
        component is closed under a cycle and therefore has no entry point.
        """
        if not self.nodes:
            return []
        outgoing = self._outgoing()
        seen: set[str] = set()
        stack = list(self.roots())
        while stack:
            current = stack.pop()
            if current in seen or current not in self.nodes:
                continue
            seen.add(current)
            stack.extend(outgoing.get(current, ()))
        return [n for n in self.nodes if n not in seen]

    def reachable(self, start: str) -> list[str]:
        if start not in self.nodes:
            raise GraphError(f"unknown node: {start}")
        outgoing = self._outgoing()
        seen: list[str] = []
        stack = [start]
        while stack:
            current = stack.pop()
            if current in seen or current not in self.nodes:
                continue
            seen.append(current)
            stack.extend(outgoing.get(current, ()))
        return seen

    def find_cycles(self) -> list[list[str]]:
        """Return each distinct directed cycle as a list of node ids."""
        outgoing = self._outgoing()
        color = {node_id: 0 for node_id in self.nodes}
        cycles: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()

        for start in self.nodes:
            if color[start] != 0:
                continue
            path = [start]
            position = {start: 0}
            color[start] = 1
            stack: list[tuple[str, int]] = [(start, 0)]
            while stack:
                node_id, index = stack[-1]
                targets = outgoing.get(node_id, ())
                if index < len(targets):
                    stack[-1] = (node_id, index + 1)
                    nxt = targets[index]
                    if nxt not in self.nodes:
                        continue
                    if color[nxt] == 0:
                        color[nxt] = 1
                        position[nxt] = len(path)
                        path.append(nxt)
                        stack.append((nxt, 0))
                    elif color[nxt] == 1:
                        cycle = path[position[nxt]:]
                        pivot = cycle.index(min(cycle))
                        key = tuple(cycle[pivot:] + cycle[:pivot])
                        if key not in seen:
                            seen.add(key)
                            cycles.append(list(key))
                else:
                    stack.pop()
                    color[node_id] = 2
                    position.pop(node_id, None)
                    path.pop()
        return cycles

    def topological_order(self) -> list[str]:
        """Kahn's algorithm, preserving node insertion order for ties."""
        unknown = [
            e
            for e in self.edges
            if e.source not in self.nodes or e.target not in self.nodes
        ]
        if unknown:
            bad = unknown[0]
            raise GraphError(
                f"edge references unknown node: {bad.source} -> {bad.target}"
            )
        outgoing = self._outgoing()
        indegree = {node_id: 0 for node_id in self.nodes}
        for edge in self.edges:
            if edge.target in indegree:
                indegree[edge.target] += 1
        queue = deque(node_id for node_id in self.nodes if indegree[node_id] == 0)
        order: list[str] = []
        while queue:
            current = queue.popleft()
            order.append(current)
            for target in outgoing.get(current, ()):
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if len(order) != len(self.nodes):
            cycle = self.find_cycles()[0]
            raise GraphError("cycle detected: " + " -> ".join(cycle + [cycle[0]]))
        return order

    # ----------------------------------------------------------- validation
    def validate(self) -> list[str]:
        """Return every structural problem, in a stable order."""
        issues: list[str] = []
        for node in self.nodes.values():
            if node.kind not in KINDS:
                issues.append(f"unknown node kind: {node.id} ({node.kind})")

        self_loops: set[str] = set()
        duplicate_edges: set[tuple[str, str, str]] = set()
        seen_edges: set[tuple[str, str, str]] = set()
        for edge in self.edges:
            if edge.source == edge.target:
                self_loops.add(edge.source)
                issues.append(f"self-loop on node: {edge.source}")
            if edge.source not in self.nodes:
                issues.append(
                    f"unknown edge source: {edge.source} -> {edge.target}"
                )
            if edge.target not in self.nodes:
                issues.append(
                    f"unknown edge target: {edge.source} -> {edge.target}"
                )
            key = (edge.source, edge.target, edge.label)
            if key in seen_edges:
                duplicate_edges.add(key)
            seen_edges.add(key)
        for source, target, label in sorted(duplicate_edges):
            suffix = f" ({label})" if label else ""
            issues.append(f"duplicate edge: {source} -> {target}{suffix}")

        for cycle in self.find_cycles():
            if len(cycle) == 1 and cycle[0] in self_loops:
                continue
            issues.append("cycle detected: " + " -> ".join(cycle + [cycle[0]]))

        orphan_set = set(self.orphans())
        for node_id in self.orphans():
            issues.append(f"orphan node: {node_id} (no edges)")
        for node_id in self.unreachable():
            if node_id not in orphan_set:
                issues.append(f"unreachable node: {node_id}")
        return issues

    @property
    def is_valid(self) -> bool:
        return not self.validate()

    # ---------------------------------------------------------- serialisation
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "nodes": [node.to_dict() for node in self.nodes.values()],
            "edges": [edge.to_dict() for edge in self.edges],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False) + "\n"

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FlowGraph":
        if not isinstance(data, Mapping):
            raise GraphError("flow must be a JSON object")
        graph = cls(
            name=str(data.get("name") or data.get("id") or "flow"),
            flow_id=str(data["id"]) if data.get("id") else None,
        )
        raw_nodes = data.get("nodes", [])
        if not isinstance(raw_nodes, list):
            raise GraphError("'nodes' must be a list")
        for raw in raw_nodes:
            node = Node.from_dict(raw)
            if node.id in graph.nodes:
                raise GraphError(f"duplicate node id: {node.id}")
            graph.nodes[node.id] = node
        raw_edges = data.get("edges", [])
        if not isinstance(raw_edges, list):
            raise GraphError("'edges' must be a list")
        graph.edges = [Edge.from_dict(raw) for raw in raw_edges]
        return graph

    @classmethod
    def load(cls, path: str | Path) -> "FlowGraph":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GraphError(f"invalid JSON in {path}: {exc}") from None
        return cls.from_dict(data)

    # ------------------------------------------------------------- reporting
    def summary(self) -> dict[str, Any]:
        issues = self.validate()
        report: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "nodes": self.node_count,
            "edges": self.edge_count,
            "valid": not issues,
            "issues": issues,
            "orphans": self.orphans(),
            "unreachable": self.unreachable(),
        }
        if not issues:
            report["order"] = self.topological_order()
        return report
