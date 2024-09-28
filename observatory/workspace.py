"""Workspace layout for flows and recorded runs.

A workspace is a directory with ``flows/`` and ``runs/`` inside it. Keeping this
in one small object means the server, the CLI and the tests agree on where files
live instead of each recomputing paths.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / "workspace"


@dataclass(frozen=True)
class Workspace:
    root: Path

    @classmethod
    def default(cls) -> "Workspace":
        return cls(DEFAULT_ROOT)

    @property
    def flows_dir(self) -> Path:
        return self.root / "flows"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    def ensure(self) -> "Workspace":
        self.flows_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        return self

    def flow_files(self) -> list[Path]:
        if not self.flows_dir.is_dir():
            return []
        return sorted(self.flows_dir.glob("*.json"))

    def run_files(self) -> list[Path]:
        if not self.runs_dir.is_dir():
            return []
        return sorted(self.runs_dir.glob("*.json"))
