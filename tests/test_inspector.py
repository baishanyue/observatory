"""Trace tree construction, flattening and text summaries."""

from __future__ import annotations

from observatory.inspector import (
    build_tree,
    flatten,
    metrics,
    summarize,
    tree_dict,
)
from observatory.runs import Run, Step


def _step(
    step_id: str,
    node_id: str,
    parent: str | None = None,
    *,
    latency: float = 100.0,
    start: float = 0.0,
    tokens_in: int = 10,
    tokens_out: int = 5,
    output: str = "ok",
) -> Step:
    return Step(
        id=step_id,
        node_id=node_id,
        label=node_id.title(),
        kind="agent",
        status="ok",
        parent=parent,
        started_ms=start,
        latency_ms=latency,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        output=output,
    )


def _run(steps: list[Step]) -> Run:
    return Run(
        id="run_tree",
        flow_id="flow",
        flow_name="Trace Flow",
        started_at="2026-02-01T00:00:00Z",
        steps=steps,
    )


def test_tree_nesting_and_depth() -> None:
    run = _run(
        [
            _step("s1", "a"),
            _step("s2", "b", "s1"),
            _step("s3", "c", "s1"),
            _step("s4", "d", "s2"),
        ]
    )
    roots = build_tree(run)
    assert len(roots) == 1
    assert roots[0].step.node_id == "a"
    assert [child.step.node_id for child in roots[0].children] == ["b", "c"]
    assert roots[0].children[0].children[0].step.node_id == "d"
    assert roots[0].children[0].children[0].depth == 2


def test_parent_can_reference_a_node_id() -> None:
    run = _run([_step("s1", "a"), _step("s2", "b", "a")])
    roots = build_tree(run)
    assert len(roots) == 1
    assert roots[0].children[0].step.id == "s2"


def test_missing_and_self_parents_become_roots() -> None:
    run = _run(
        [
            _step("s1", "a"),
            _step("s2", "b", "missing"),
            _step("s3", "c", "s3"),
        ]
    )
    roots = build_tree(run)
    assert [root.step.id for root in roots] == ["s1", "s2", "s3"]


def test_parent_cycle_is_broken_safely() -> None:
    run = _run(
        [
            _step("s1", "a", "s2"),
            _step("s2", "b", "s1"),
        ]
    )
    roots = build_tree(run)
    seen = [node.step.id for node in flatten(roots)]
    assert sorted(seen) == ["s1", "s2"]
    assert len(roots) == 1


def test_flatten_is_preorder() -> None:
    run = _run(
        [
            _step("s1", "a"),
            _step("s2", "b", "s1"),
            _step("s3", "c", "s1"),
            _step("s4", "d", "s2"),
        ]
    )
    assert [node.step.id for node in flatten(build_tree(run))] == [
        "s1",
        "s2",
        "s4",
        "s3",
    ]


def test_summary_contains_metrics_and_tree() -> None:
    run = _run(
        [
            _step("s1", "a", latency=120, tokens_in=100, tokens_out=20),
            _step("s2", "b", "s1", latency=340, start=120, tokens_in=200, tokens_out=40),
        ]
    )
    text = summarize(run)
    assert "Trace Flow" in text
    assert "2 steps" in text
    assert "460ms" in text
    assert "360 tok" in text
    assert "│" in text or "└─" in text
    clipped = summarize(run, max_steps=1)
    assert "1 more steps" in clipped


def test_tree_dict_and_metrics() -> None:
    run = _run([_step("s1", "a"), _step("s2", "b", "s1")])
    payload = tree_dict(build_tree(run))
    assert payload[0]["step"]["id"] == "s1"
    assert payload[0]["children"][0]["step"]["id"] == "s2"
    stats = metrics(run)
    assert stats["steps"] == 2
    assert stats["root_steps"] == 1
    assert stats["tokens"] == 30
    assert stats["ok"] == 2
