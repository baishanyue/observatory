"""Surface checks: CLI, docs self-containment and example consistency."""

from __future__ import annotations

from pathlib import Path

import pytest

from observatory import FlowGraph, Run
from observatory.cli import main as cli_main

ROOT = Path(__file__).resolve().parents[1]


def test_cli_help_and_validate(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_main(["--help"])
    assert exc.value.code == 0
    assert "validate" in capsys.readouterr().out
    assert cli_main(["validate", "workspace/flows/research-agent.json"]) == 0
    assert "valid" in capsys.readouterr().out


def test_cli_summary_and_invalid_flow(tmp_path: Path, capsys) -> None:
    assert cli_main(["summary", "workspace/runs/run_7fc21a9d04.json"]) == 0
    assert "4,486 tok" in capsys.readouterr().out

    broken = tmp_path / "broken.json"
    broken.write_text(
        '{"id":"bad","name":"Bad","nodes":[{"id":"a","kind":"agent"},'
        '{"id":"b","kind":"agent"}],"edges":[{"source":"a","target":"missing"}]}',
        encoding="utf-8",
    )
    assert cli_main(["validate", str(broken)]) == 1
    assert "unknown edge target" in capsys.readouterr().out


def test_ui_and_docs_are_self_contained() -> None:
    for relative in ("web/index.html", "docs/index.html"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "OBSERVATORY" in text
        assert "http://" not in text
        assert "https://" not in text
        for word in ("TO" + "DO", "lo" + "rem", "coming " + "soon"):
            assert word not in text


def test_example_run_matches_example_flow() -> None:
    flow = FlowGraph.load(ROOT / "workspace/flows/research-agent.json")
    run = Run.load(ROOT / "workspace/runs/run_7fc21a9d04.json")
    assert run.flow_id == flow.id
    assert {step.node_id for step in run.steps} == set(flow.nodes)
    assert run.validate() == []
    for step in run.steps:
        assert step.kind == flow.nodes[step.node_id].kind


def test_readme_documents_the_workbench() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    headings = [
        "## The three panes",
        "## Run it locally",
        "## The two documents it reads",
        "## HTTP surface",
        "## Replay and diff",
        "## Performance",
        "## What it leaves out",
    ]
    positions = [text.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "assets/workbench.png" in text
    assert "## License" not in text
