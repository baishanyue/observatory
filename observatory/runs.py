"""Run records, durable storage, replay and structural diffs.

A run is an ordered list of :class:`Step` events.  Timings are relative
milliseconds from the start of the run, which keeps replay cheap and makes
two runs directly comparable regardless of wall-clock time.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .graph import FlowGraph

__all__ = [
    "STATUSES",
    "RunError",
    "ReplayError",
    "StoreError",
    "Step",
    "Run",
    "RunStore",
    "new_run_id",
    "replay_run",
    "diff_runs",
    "format_diff",
]

STATUSES = frozenset({"ok", "error", "skipped", "running"})

_RUN_PREFIX = "run_"


class RunError(ValueError):
    """Base error for run-level operations."""


class ReplayError(RunError):
    """Raised when a run cannot be replayed against a flow."""


class StoreError(RunError):
    """Raised when a run store cannot be read or written."""


def new_run_id() -> str:
    return _RUN_PREFIX + uuid.uuid4().hex[:10]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_after(started_at: str, duration_ms: float) -> str:
    try:
        base = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        base = _utc_now()
    return _iso(base + timedelta(milliseconds=max(0.0, duration_ms)))


@dataclass(slots=True)
class Step:
    """One recorded unit of work inside a run."""

    id: str
    node_id: str
    label: str
    kind: str = "agent"
    status: str = "ok"
    parent: str | None = None
    started_ms: float = 0.0
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    output: str = ""
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise RunError("step id must be a non-empty string")
        if not isinstance(self.node_id, str) or not self.node_id.strip():
            raise RunError(f"step {self.id!r} needs a node_id")
        if not isinstance(self.label, str) or not self.label:
            self.label = self.node_id
        self.started_ms = float(self.started_ms)
        self.latency_ms = float(self.latency_ms)
        self.tokens_in = int(self.tokens_in)
        self.tokens_out = int(self.tokens_out)

    @property
    def ended_ms(self) -> float:
        return self.started_ms + self.latency_ms

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "node_id": self.node_id,
            "label": self.label,
            "kind": self.kind,
            "status": self.status,
            "parent": self.parent,
            "started_ms": round(self.started_ms, 3),
            "ended_ms": round(self.ended_ms, 3),
            "latency_ms": round(self.latency_ms, 3),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "output": self.output,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Step":
        if not isinstance(data, Mapping):
            raise RunError("step entry must be an object")
        try:
            started = float(data.get("started_ms", 0.0))
            if "latency_ms" in data:
                latency = float(data["latency_ms"])
            elif "ended_ms" in data:
                latency = float(data["ended_ms"]) - started
            else:
                latency = 0.0
            return cls(
                id=str(data["id"]),
                node_id=str(data["node_id"]),
                label=str(data.get("label", data["node_id"])),
                kind=str(data.get("kind", "agent")),
                status=str(data.get("status", "ok")),
                parent=str(data["parent"]) if data.get("parent") else None,
                started_ms=started,
                latency_ms=max(0.0, latency),
                tokens_in=int(data.get("tokens_in", 0)),
                tokens_out=int(data.get("tokens_out", 0)),
                output=str(data.get("output", "")),
                error=data.get("error"),
            )
        except KeyError as exc:
            raise RunError(f"step is missing {exc.args[0]!r}") from None


@dataclass(slots=True)
class Run:
    """A complete or partial execution of a flow."""

    id: str
    flow_id: str
    flow_name: str = ""
    status: str = "completed"
    started_at: str = ""
    finished_at: str | None = None
    replayed_from: str | None = None
    steps: list[Step] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise RunError("run id must be a non-empty string")
        if not isinstance(self.flow_id, str) or not self.flow_id.strip():
            raise RunError(f"run {self.id!r} needs a flow_id")
        if not self.flow_name:
            self.flow_name = self.flow_id
        if not self.started_at:
            self.started_at = _iso(_utc_now())
        self.steps = list(self.steps)
        self.metadata = dict(self.metadata)

    # ------------------------------------------------------------- metrics
    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def duration_ms(self) -> float:
        if not self.steps:
            return 0.0
        start = min(s.started_ms for s in self.steps)
        end = max(s.ended_ms for s in self.steps)
        return round(end - start, 3)

    @property
    def total_latency_ms(self) -> float:
        return round(sum(s.latency_ms for s in self.steps), 3)

    @property
    def tokens_in(self) -> int:
        return sum(s.tokens_in for s in self.steps)

    @property
    def tokens_out(self) -> int:
        return sum(s.tokens_out for s in self.steps)

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def step(self, step_id: str) -> Step:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise RunError(f"unknown step: {step_id}")

    def status_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in sorted(STATUSES)}
        for step in self.steps:
            counts[step.status] = counts.get(step.status, 0) + 1
        return counts

    def metrics(self) -> dict[str, Any]:
        return {
            "steps": self.step_count,
            "duration_ms": self.duration_ms,
            "total_latency_ms": self.total_latency_ms,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "total_tokens": self.total_tokens,
            "status_counts": self.status_counts(),
        }

    def validate(self) -> list[str]:
        issues: list[str] = []
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                issues.append(f"duplicate step id: {step.id}")
            seen.add(step.id)
            if step.status not in STATUSES:
                issues.append(f"unknown step status: {step.id} ({step.status})")
        if self.steps and self.status not in {
            "completed",
            "failed",
            "running",
            "replayed",
        }:
            issues.append(f"unknown run status: {self.status}")
        step_ids = {s.id for s in self.steps}
        node_ids = {s.node_id for s in self.steps}
        for step in self.steps:
            if step.parent and step.parent not in step_ids and step.parent not in node_ids:
                issues.append(f"step {step.id} has unknown parent: {step.parent}")
        return issues

    # -------------------------------------------------------- serialisation
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "flow_id": self.flow_id,
            "flow_name": self.flow_name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "replayed_from": self.replayed_from,
            "steps": [step.to_dict() for step in self.steps],
            "metadata": self.metadata,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False) + "\n"

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Run":
        if not isinstance(data, Mapping):
            raise RunError("run must be a JSON object")
        try:
            steps = [Step.from_dict(raw) for raw in data.get("steps", [])]
            run = cls(
                id=str(data["id"]),
                flow_id=str(data["flow_id"]),
                flow_name=str(data.get("flow_name", data["flow_id"])),
                status=str(data.get("status", "completed")),
                started_at=str(data.get("started_at", "")),
                finished_at=data.get("finished_at"),
                replayed_from=data.get("replayed_from"),
                steps=steps,
                metadata=data.get("metadata", {}),
            )
        except KeyError as exc:
            raise RunError(f"run is missing {exc.args[0]!r}") from None
        return run

    @classmethod
    def load(cls, path: str | Path) -> "Run":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RunError(f"invalid JSON in {path}: {exc}") from None
        return cls.from_dict(data)

    def with_status(self, status: str) -> "Run":
        clone = replace(self)
        clone.status = status
        return clone


def replay_run(
    run: Run,
    *,
    replay_id: str | None = None,
    flow: FlowGraph | None = None,
) -> Run:
    """Replay ``run`` deterministically.

    The workbench has no model dependency, so replay re-emits the recorded step
    sequence under a fresh run id.  Passing a flow enables a structural check:
    a replay that references a node absent from the flow is rejected.
    """
    if flow is not None:
        missing = sorted({s.node_id for s in run.steps if s.node_id not in flow.nodes})
        if missing:
            raise ReplayError(
                "run references nodes outside the flow: " + ", ".join(missing)
            )
    new_id = replay_id or new_run_id()
    started = _iso(_utc_now())
    clone = Run(
        id=new_id,
        flow_id=run.flow_id,
        flow_name=run.flow_name,
        status="replayed",
        started_at=started,
        finished_at=_iso_after(started, run.duration_ms),
        replayed_from=run.id,
        steps=[replace(step) for step in run.steps],
        metadata={**run.metadata, "replay": True},
    )
    return clone


# ---------------------------------------------------------------- run store
class RunStore:
    """A list of runs, optionally persisted to one JSON file."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path: Path | None = Path(path) if path is not None else None
        self._runs: dict[str, Run] = {}
        if self.path is not None and self.path.exists():
            self._load()

    def __len__(self) -> int:
        return len(self._runs)

    def __iter__(self) -> Iterator[Run]:
        return iter(self.list_runs())

    def __contains__(self, run_id: object) -> bool:
        return run_id in self._runs

    def add(self, run: Run) -> Run:
        if run.id in self._runs:
            raise StoreError(f"duplicate run id: {run.id}")
        self._runs[run.id] = run
        if self.path is not None:
            self.save()
        return run

    def add_many(self, runs: Iterable[Run]) -> None:
        for run in runs:
            if run.id in self._runs:
                raise StoreError(f"duplicate run id: {run.id}")
            self._runs[run.id] = run
        if self.path is not None:
            self.save()

    def get(self, run_id: str) -> Run:
        try:
            return self._runs[run_id]
        except KeyError:
            raise KeyError(f"unknown run: {run_id}") from None

    def find(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def remove(self, run_id: str) -> Run:
        run = self.get(run_id)
        del self._runs[run_id]
        if self.path is not None:
            self.save()
        return run

    def list_runs(self) -> list[Run]:
        # Reverse insertion first so equal timestamps still read newest-first.
        runs = list(self._runs.values())[::-1]
        runs.sort(key=lambda run: run.started_at, reverse=True)
        return runs

    def summary(self) -> list[dict[str, Any]]:
        return [
            {
                "id": run.id,
                "flow_id": run.flow_id,
                "flow_name": run.flow_name,
                "status": run.status,
                "started_at": run.started_at,
                "steps": run.step_count,
                "duration_ms": run.duration_ms,
                "total_tokens": run.total_tokens,
                "replayed_from": run.replayed_from,
            }
            for run in self.list_runs()
        ]

    def replay(self, run_id: str, *, flow: FlowGraph | None = None) -> Run:
        source = self.get(run_id)
        new_id = new_run_id()
        while new_id in self._runs:
            new_id = new_run_id()
        clone = replay_run(source, replay_id=new_id, flow=flow)
        return self.add(clone)

    def diff(self, left_id: str, right_id: str) -> dict[str, Any]:
        return diff_runs(self.get(left_id), self.get(right_id))

    # -------------------------------------------------------- persistence
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "runs": [run.to_dict() for run in self._runs.values()],
        }

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise StoreError("store has no path; pass one to save()")
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, delete=False, suffix=".tmp"
        )
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, target)
        except Exception:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise
        self.path = target
        return target

    def _load(self) -> None:
        assert self.path is not None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StoreError(f"invalid JSON in {self.path}: {exc}") from None
        if isinstance(data, list):
            raw_runs = data
        elif isinstance(data, Mapping) and "runs" in data:
            raw_runs = data["runs"]
        elif isinstance(data, Mapping) and "id" in data and "steps" in data:
            raw_runs = [data]
        else:
            raise StoreError(f"unrecognised run store format: {self.path}")
        if not isinstance(raw_runs, list):
            raise StoreError(f"'runs' must be a list in {self.path}")
        for raw in raw_runs:
            run = Run.from_dict(raw)
            if run.id in self._runs:
                raise StoreError(f"duplicate run id in store: {run.id}")
            self._runs[run.id] = run

    def reload(self) -> "RunStore":
        self._runs.clear()
        if self.path is not None and self.path.exists():
            self._load()
        return self


# -------------------------------------------------------------------- diff
def _step_keys(steps: Iterable[Step]) -> list[str]:
    counts: dict[str, int] = {}
    keys: list[str] = []
    for step in steps:
        count = counts.get(step.node_id, 0) + 1
        counts[step.node_id] = count
        keys.append(step.node_id if count == 1 else f"{step.node_id}#{count}")
    return keys


def _brief(step: Step) -> dict[str, Any]:
    return {
        "node_id": step.node_id,
        "label": step.label,
        "status": step.status,
        "latency_ms": round(step.latency_ms, 3),
        "tokens": step.tokens,
    }


def diff_runs(left: Run, right: Run) -> dict[str, Any]:
    """Compare two runs structurally, keyed by node occurrence."""
    left_keys = _step_keys(left.steps)
    right_keys = _step_keys(right.steps)
    left_map = dict(zip(left_keys, left.steps))
    right_map = dict(zip(right_keys, right.steps))

    added = [_brief(right_map[k]) for k in right_keys if k not in left_map]
    removed = [_brief(left_map[k]) for k in left_keys if k not in right_map]
    shared_left = [k for k in left_keys if k in right_map]
    shared_right = [k for k in right_keys if k in left_map]
    order_changed = shared_left != shared_right

    changed: list[dict[str, Any]] = []
    unchanged = 0
    for key in shared_left:
        before, after = left_map[key], right_map[key]
        fields: dict[str, Any] = {}
        if before.status != after.status:
            fields["status"] = {"from": before.status, "to": after.status}
        if round(before.latency_ms, 3) != round(after.latency_ms, 3):
            fields["latency_ms"] = {
                "from": round(before.latency_ms, 3),
                "to": round(after.latency_ms, 3),
                "delta": round(after.latency_ms - before.latency_ms, 3),
            }
        if before.tokens_in != after.tokens_in:
            fields["tokens_in"] = {"from": before.tokens_in, "to": after.tokens_in}
        if before.tokens_out != after.tokens_out:
            fields["tokens_out"] = {"from": before.tokens_out, "to": after.tokens_out}
        if before.parent != after.parent:
            fields["parent"] = {"from": before.parent, "to": after.parent}
        if before.output != after.output:
            fields["output"] = {
                "from": before.output[:160],
                "to": after.output[:160],
            }
        if fields:
            changed.append(
                {
                    "node_id": before.node_id,
                    "key": key,
                    "label": before.label,
                    "fields": fields,
                }
            )
        else:
            unchanged += 1

    report = {
        "left_id": left.id,
        "right_id": right.id,
        "left_status": left.status,
        "right_status": right.status,
        "flow_changed": left.flow_id != right.flow_id,
        "step_count": {"left": left.step_count, "right": right.step_count},
        "added": added,
        "removed": removed,
        "changed": changed,
        "order_changed": order_changed,
        "unchanged": unchanged,
        "metrics": {
            "left": left.metrics(),
            "right": right.metrics(),
            "token_delta": right.total_tokens - left.total_tokens,
            "latency_delta_ms": round(right.duration_ms - left.duration_ms, 3),
        },
    }
    return report


def format_diff(report: Mapping[str, Any]) -> str:
    """Render a compact human-readable diff."""
    lines = [
        f"{report['left_id']}  ->  {report['right_id']}",
        (
            f"steps {report['step_count']['left']} -> {report['step_count']['right']}"
            f"   tokens {report['metrics']['left']['total_tokens']}"
            f" -> {report['metrics']['right']['total_tokens']}"
            f"   latency {report['metrics']['left']['duration_ms']:.0f}ms"
            f" -> {report['metrics']['right']['duration_ms']:.0f}ms"
        ),
    ]
    if report["flow_changed"]:
        lines.append("flow changed")
    for item in report["removed"]:
        lines.append(f"- {item['node_id']}  ({item['label']})")
    for item in report["added"]:
        lines.append(f"+ {item['node_id']}  ({item['label']})")
    for item in report["changed"]:
        parts = [
            f"{name} {change['from']} -> {change['to']}"
            for name, change in item["fields"].items()
        ]
        lines.append(f"~ {item['node_id']}  " + "; ".join(parts))
    if report["order_changed"]:
        lines.append("step order changed")
    if not (report["added"] or report["removed"] or report["changed"]):
        lines.append("structurally identical")
    return "\n".join(lines)
