from pathlib import Path

from observatory.workspace import Workspace


def test_default_workspace_matches_repo_layout():
    ws = Workspace.default()
    assert ws.flows_dir.name == "flows"
    assert ws.runs_dir.name == "runs"
    assert (ws.flows_dir / "research-agent.json").exists()
    assert list(ws.runs_dir.glob("*.json"))


def test_ensure_creates_both_directories(tmp_path: Path):
    ws = Workspace(tmp_path / "ws").ensure()
    assert ws.flows_dir.is_dir()
    assert ws.runs_dir.is_dir()


def test_missing_directories_yield_empty_lists(tmp_path: Path):
    ws = Workspace(tmp_path / "absent")
    assert ws.flow_files() == []
    assert ws.run_files() == []
