"""Graph construction, validation, ordering and serialisation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from observatory.graph import Edge, FlowGraph, GraphError, Node

WORKSPACE = Path(__file__).resolve().parents[1] / "workspace"


def _diamond() -> FlowGraph:
    graph = FlowGraph("Diamond", "diamond")
    graph.add_node("start", "input", "Start")
    graph.add_node("left", "agent", "Left")
    graph.add_node("right", "agent", "Right")
    graph.add_node("end", "output", "End")
    graph.add_edge("start", "left")
    graph.add_edge("start", "right")
    graph.add_edge("left", "end")
    graph.add_edge("right", "end")
    return graph


def test_topological_order_is_stable() -> None:
    graph = _diamond()
    assert graph.topological_order() == ["start", "left", "right", "end"]
    assert graph.roots() == ["start"]
    assert graph.leaves() == ["end"]
    assert graph.orphans() == []
    assert graph.unreachable() == []
    assert graph.is_valid


def test_unknown_edge_reference_is_reported() -> None:
    graph = _diamond()
    graph.add_edge("left", "ghost")
    issues = graph.validate()
    assert any("unknown edge target" in issue and "ghost" in issue for issue in issues)
    with pytest.raises(GraphError, match="unknown node"):
        graph.topological_order()


def test_cycle_detection_reports_the_cycle() -> None:
    graph = _diamond()
    graph.add_edge("end", "left")
    issues = graph.validate()
    assert any(issue.startswith("cycle detected:") for issue in issues)
    with pytest.raises(GraphError, match="cycle detected"):
        graph.topological_order()


def test_orphan_and_unreachable_nodes() -> None:
    graph = _diamond()
    graph.add_node("island", "agent", "Island")
    assert graph.orphans() == ["island"]
    assert any("orphan node: island" in issue for issue in graph.validate())

    cyclic = FlowGraph("Cyclic")
    cyclic.add_node("a", "input", "A")
    cyclic.add_node("b", "agent", "B")
    cyclic.add_node("c", "agent", "C")
    cyclic.add_node("d", "agent", "D")
    cyclic.add_edge("a", "b")
    cyclic.add_edge("c", "d")
    cyclic.add_edge("d", "c")
    assert set(cyclic.unreachable()) == {"c", "d"}
    issues = cyclic.validate()
    assert any("unreachable node" in issue for issue in issues)
    assert any("cycle detected" in issue for issue in issues)


def test_self_loop_is_reported_once() -> None:
    graph = FlowGraph("Loop")
    graph.add_node("a", "agent", "A")
    graph.add_edge("a", "a")
    issues = graph.validate()
    assert issues.count("self-loop on node: a") == 1
    assert not any(issue.startswith("cycle detected") for issue in issues)


def test_duplicate_and_unknown_kind_validation() -> None:
    graph = _diamond()
    graph.add_edge("start", "left")
    graph.nodes["left"].kind = "teleporter"
    issues = graph.validate()
    assert any("duplicate edge: start -> left" in issue for issue in issues)
    assert any("unknown node kind: left" in issue for issue in issues)


def test_node_and_edge_roundtrip(tmp_path: Path) -> None:
    graph = _diamond()
    payload = graph.to_dict()
    restored = FlowGraph.from_dict(json.loads(json.dumps(payload)))
    assert restored.to_dict() == payload
    assert restored.id == "diamond"
    assert restored.name == "Diamond"
    assert isinstance(restored.edges[0], Edge)
    assert isinstance(restored.nodes["start"], Node)
    assert restored.nodes["start"].config == {}

    target = tmp_path / "flow.json"
    graph.save(target)
    assert FlowGraph.load(target).to_dict() == payload
    assert target.read_text(encoding="utf-8").endswith("\n")


def test_duplicate_node_is_rejected() -> None:
    graph = _diamond()
    with pytest.raises(GraphError, match="duplicate node id"):
        graph.add_node("start", "input", "Again")
    with pytest.raises(GraphError, match="duplicate node id"):
        FlowGraph.from_dict(
            {
                "nodes": [
                    {"id": "a", "kind": "input"},
                    {"id": "a", "kind": "output"},
                ],
                "edges": [],
            }
        )


def test_reachable_and_remove_node() -> None:
    graph = _diamond()
    assert set(graph.reachable("left")) == {"left", "end"}
    graph.remove_node("right")
    assert graph.edge_count == 2
    assert graph.topological_order() == ["start", "left", "end"]


def test_example_flow_is_valid() -> None:
    graph = FlowGraph.load(WORKSPACE / "flows" / "research-agent.json")
    assert graph.id == "research-agent"
    assert graph.node_count == 7
    assert graph.edge_count == 8
    assert graph.is_valid, graph.validate()
    assert graph.topological_order()[0] == "intake"
    assert graph.topological_order()[-1] == "reply"
    assert graph.summary()["valid"] is True
