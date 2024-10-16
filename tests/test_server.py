"""API facade, simulation and the stdlib HTTP transport."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from observatory.graph import FlowGraph
from observatory.runs import RunStore
from observatory.server import (
    Studio,
    create_server,
    load_example_flows,
    simulate_run,
)

WORKSPACE = Path(__file__).resolve().parents[1] / "workspace"


def _studio() -> Studio:
    flows = load_example_flows(WORKSPACE / "flows")
    return Studio(flows=flows.values(), store=RunStore())


def _request(connection: http.client.HTTPConnection, method: str, path: str, payload=None):
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    return response.status, raw


def test_simulate_run_is_deterministic() -> None:
    graph = FlowGraph.load(WORKSPACE / "flows" / "research-agent.json")
    first = simulate_run(graph)
    second = simulate_run(graph)
    assert first.step_count == 7
    assert first.status == "completed"
    assert first.duration_ms == second.duration_ms
    assert [s.latency_ms for s in first.steps] == [s.latency_ms for s in second.steps]
    assert [s.status for s in first.steps] == ["ok"] * 7
    assert first.steps[-1].latency_ms == 210


def test_simulate_failure_skips_downstream() -> None:
    graph = FlowGraph.load(WORKSPACE / "flows" / "research-agent.json")
    run = simulate_run(graph, fail_node="guardrail")
    assert run.status == "failed"
    statuses = {step.node_id: step.status for step in run.steps}
    assert statuses["guardrail"] == "error"
    assert statuses["reply"] == "skipped"
    assert statuses["synthesize"] == "ok"


def test_server_api_end_to_end(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>OBSERVATORY</title>", encoding="utf-8")
    studio = _studio()
    server = create_server(studio, port=0, static_dir=static)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        status, raw = _request(connection, "GET", "/api/flows")
        assert status == 200
        payload = json.loads(raw)
        assert payload["flows"][0]["id"] == "research-agent"
        assert len(payload["flows"][0]["nodes"]) == 7

        status, raw = _request(connection, "GET", "/api/runs")
        assert status == 200
        assert json.loads(raw)["runs"] == []

        status, raw = _request(
            connection, "POST", "/api/runs", {"flow_id": "research-agent"}
        )
        assert status == 201
        created = json.loads(raw)["run"]
        assert created["flow_id"] == "research-agent"
        assert len(created["steps"]) == 7

        status, raw = _request(connection, "GET", "/api/runs")
        assert json.loads(raw)["latest"] == created["id"]

        status, raw = _request(
            connection, "POST", "/api/replay", {"run_id": created["id"]}
        )
        assert status == 201
        replayed = json.loads(raw)["run"]
        assert replayed["replayed_from"] == created["id"]
        assert replayed["status"] == "replayed"

        status, raw = _request(connection, "GET", "/")
        assert status == 200
        assert b"OBSERVATORY" in raw

        status, raw = _request(connection, "GET", "/..%2fREADME.md")
        assert status == 404

        status, raw = _request(connection, "GET", "/api/nope")
        assert status == 404
        assert "error" in json.loads(raw)
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_api_validation_errors(tmp_path: Path) -> None:
    studio = _studio()
    server = create_server(studio, port=0, static_dir=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        status, raw = _request(
            connection, "POST", "/api/runs", {"flow_id": "missing-flow"}
        )
        assert status == 400
        assert "unknown flow" in json.loads(raw)["error"]
        status, raw = _request(connection, "POST", "/api/replay", {})
        assert status == 400
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
