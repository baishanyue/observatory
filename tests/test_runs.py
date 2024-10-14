"""Run records, storage, replay and structural diffs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from observatory.graph import FlowGraph
from observatory.runs import (
    ReplayError,
    Run,
    RunStore,
    Step,
    diff_runs,
    format_diff,
    replay_run,
)

WORKSPACE = Path(__file__).resolve().parents[1] / "workspace"


def _step(
    step_id: str,
    node_id: str,
    *,
    status: str = "ok",
    latency: float = 100.0,
    start: float = 0.0,
    tin: int = 10,
    tout: int = 20,
    parent: str | None = None,
) -> Step:
    return Step(
        id=step_id,
        node_id=node_id,
        label=node_id.title(),
        kind="agent",
        status=status,
        parent=parent,
        started_ms=start,
        latency_ms=latency,
        tokens_in=tin,
        tokens_out=tout,
        output=f"{node_id} output",
    )


def _run(run_id: str = "run_test", steps: list[Step] | None = None) -> Run:
    return Run(
        id=run_id,
        flow_id="flow",
        flow_name="Flow",
        status="completed",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:02Z",
        steps=steps
        or [
            _step("s1", "a", latency=100),
            _step("s2", "b", latency=200, start=100),
            _step("s3", "c", latency=300, start=300),
        ],
    )


def test_metrics_and_status_counts() -> None:
    run = _run()
    assert run.step_count == 3
    assert run.duration_ms == 600
    assert run.total_latency_ms == 600
    assert run.total_tokens == 90
    assert run.status_counts()["ok"] == 3
    assert run.metrics()["steps"] == 3


def test_example_run_roundtrip() -> None:
    run = Run.load(WORKSPACE / "runs" / "run_7fc21a9d04.json")
    assert run.id == "run_7fc21a9d04"
    assert run.step_count == 7
    assert run.total_tokens == 4486
    assert run.duration_ms == 2188
    payload = run.to_dict()
    assert Run.from_dict(json.loads(json.dumps(payload))).to_dict() == payload
    assert run.validate() == []


def test_step_accepts_ended_ms() -> None:
    step = Step.from_dict(
        {"id": "s", "node_id": "n", "started_ms": 50, "ended_ms": 175}
    )
    assert step.latency_ms == 125
    assert step.ended_ms == 175


def test_store_persists_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "runs.json"
    store = RunStore(path)
    store.add(_run("run_a"))
    store.add(
        Run(
            id="run_b",
            flow_id="flow",
            flow_name="Flow",
            started_at="2026-01-02T00:00:00Z",
            steps=[_step("s1", "a")],
        )
    )
    assert len(store) == 2
    assert path.exists()
    assert path.read_text(encoding="utf-8").endswith("\n")

    reloaded = RunStore(path)
    assert len(reloaded) == 2
    assert reloaded.get("run_a").flow_name == "Flow"
    assert [run.id for run in reloaded.list_runs()] == ["run_b", "run_a"]
    assert {item["id"] for item in reloaded.summary()} == {"run_a", "run_b"}

    reloaded.remove("run_a")
    assert len(RunStore(path)) == 1


def test_store_loads_a_single_run_file(tmp_path: Path) -> None:
    path = tmp_path / "one.json"
    _run("run_single").save(path)
    store = RunStore(path)
    assert len(store) == 1
    assert store.get("run_single").step_count == 3


def test_replay_is_deterministic_and_original_is_untouched() -> None:
    source = _run()
    clone = replay_run(source, replay_id="run_replay")
    assert clone.id == "run_replay"
    assert clone.replayed_from == source.id
    assert clone.status == "replayed"
    assert clone.metadata["replay"] is True
    assert [(s.id, s.status, s.latency_ms) for s in clone.steps] == [
        (s.id, s.status, s.latency_ms) for s in source.steps
    ]
    assert source.status == "completed"
    assert source.replayed_from is None


def test_replay_rejects_unknown_flow_nodes() -> None:
    graph = FlowGraph("Small", "small")
    graph.add_node("a", "input", "A")
    graph.add_node("b", "output", "B")
    graph.add_edge("a", "b")
    source = _run(steps=[_step("s1", "a"), _step("s2", "ghost")])
    with pytest.raises(ReplayError, match="ghost"):
        replay_run(source, flow=graph)


def test_store_replay_adds_a_new_run() -> None:
    store = RunStore()
    store.add(_run("run_source"))
    clone = store.replay("run_source")
    assert clone.id != "run_source"
    assert len(store) == 2
    assert store.get(clone.id).replayed_from == "run_source"


def test_diff_reports_added_removed_and_changed() -> None:
    left = _run(
        steps=[
            _step("s1", "a"),
            _step("s2", "b", latency=200, start=100),
            _step("s3", "c", start=300),
        ]
    )
    right = _run(
        steps=[
            _step("s1", "a"),
            _step("s2", "b", latency=260, start=100, tin=40),
            _step("s4", "d", start=360),
        ]
    )
    report = diff_runs(left, right)
    assert [item["node_id"] for item in report["removed"]] == ["c"]
    assert [item["node_id"] for item in report["added"]] == ["d"]
    changed = {item["node_id"]: item["fields"] for item in report["changed"]}
    assert changed["b"]["latency_ms"]["delta"] == 60
    assert changed["b"]["tokens_in"] == {"from": 10, "to": 40}
    assert report["step_count"] == {"left": 3, "right": 3}
    assert report["metrics"]["token_delta"] == right.total_tokens - left.total_tokens
    text = format_diff(report)
    assert "- c" in text and "+ d" in text and "~ b" in text


def test_diff_detects_order_and_status_changes() -> None:
    left = _run(steps=[_step("s1", "a"), _step("s2", "b")])
    right = _run(
        steps=[_step("s2", "b", status="error"), _step("s1", "a")],
        run_id="run_other",
    )
    report = diff_runs(left, right)
    assert report["order_changed"] is True
    assert report["changed"][0]["fields"]["status"] == {"from": "ok", "to": "error"}
    assert report["left_id"] == "run_test"


def test_identical_diff_is_quiet() -> None:
    run = _run()
    report = diff_runs(run, run)
    assert report["added"] == [] and report["removed"] == [] and report["changed"] == []
    assert "structurally identical" in format_diff(report)
