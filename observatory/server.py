"""Standard-library HTTP server for the Observatory workbench.

The server exposes a small JSON API over a set of :class:`FlowGraph` objects
and a :class:`RunStore`, then serves the single-file UI from ``static/``.
It binds to the loopback interface unless told otherwise.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlsplit

from .graph import FlowGraph, GraphError
from .workspace import Workspace
from .inspector import summarize
from .runs import Run, RunStore, Step, StoreError, _iso, _iso_after, _utc_now, new_run_id

__all__ = [
    "ROOT",
    "DEFAULT_EXAMPLES",
    "STATIC_DIR",
    "Studio",
    "StudioHTTPServer",
    "create_server",
    "load_example_flows",
    "seed_studio",
    "serve",
    "simulate_run",
]

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXAMPLES = Workspace.default().flows_dir
STATIC_DIR = ROOT / "web"
DEFAULT_DATA = Path(
    os.environ.get("OBSERVATORY_DATA", ".observatory/runs.json")
)
_VERSION = "0.2.0"

_OUTPUTS = {
    "input": "request accepted · goal parsed",
    "retriever": "8 passages · 3 sources ranked",
    "router": "branch plan: retrieve + tools",
    "tool": "2 tool calls returned · 1 skipped",
    "agent": "draft composed from retrieved evidence",
    "judge": "citation check passed · policy clear",
    "memory": "context window compacted",
    "output": "final answer emitted",
}


def _digest(*parts: object) -> int:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return int(hashlib.sha256(payload).hexdigest()[:12], 16)


def _number(value: object, fallback: int) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def simulate_run(
    graph: FlowGraph,
    *,
    run_id: str | None = None,
    fail_node: str | None = None,
) -> Run:
    """Deterministically synthesise a realistic run for ``graph``."""
    issues = graph.validate()
    if issues:
        raise GraphError("cannot run an invalid flow: " + "; ".join(issues))
    if fail_node is not None and fail_node not in graph.nodes:
        raise GraphError(f"unknown fail node: {fail_node}")

    order = graph.topological_order()
    steps: list = []
    end_by_node: dict[str, float] = {}
    status_by_node: dict[str, str] = {}
    step_id_by_node: dict[str, str] = {}
    started = _iso(_utc_now())

    for index, node_id in enumerate(order):
        node = graph.nodes[node_id]
        seed = _digest(graph.id, node.id, node.kind)
        predecessors = [e.source for e in graph.edges if e.target == node_id]
        earliest = 0.0
        for predecessor in predecessors:
            if predecessor in end_by_node:
                earliest = max(earliest, end_by_node[predecessor])

        blocked = any(
            status_by_node.get(predecessor) in {"error", "skipped"}
            for predecessor in predecessors
        )
        status = "skipped" if blocked else ("error" if node_id == fail_node else "ok")
        latency = 0.0 if status == "skipped" else float(
            _number(node.config.get("latency_ms"), 60 + seed % 260)
        )
        tokens_in = 0 if status == "skipped" else _number(
            node.config.get("tokens_in"), 80 + (seed >> 8) % 420
        )
        tokens_out = 0 if status == "skipped" else _number(
            node.config.get("tokens_out"), 24 + (seed >> 16) % 260
        )
        parent_node = None
        if predecessors:
            parent_node = max(
                predecessors,
                key=lambda item: end_by_node.get(item, 0.0),
            )
        step_id = f"s{index + 1:02d}-{node.id}"
        step_id_by_node[node.id] = step_id
        step = Step(
            id=step_id,
            node_id=node.id,
            label=node.label,
            kind=node.kind,
            status=status,
            parent=step_id_by_node.get(parent_node) if parent_node else None,
            started_ms=earliest,
            latency_ms=latency,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            output="" if status == "skipped" else _output_for(node, status),
            error="simulated node failure" if status == "error" else None,
        )
        steps.append(step)
        end_by_node[node.id] = earliest + latency
        status_by_node[node.id] = status

    duration = max(end_by_node.values(), default=0.0)
    run_status = "failed" if any(s.status == "error" for s in steps) else "completed"
    return Run(
        id=run_id or new_run_id(),
        flow_id=graph.id,
        flow_name=graph.name,
        status=run_status,
        started_at=started,
        finished_at=_iso_after(started, duration),
        steps=steps,
        metadata={"mode": "simulate", "seed": _digest(graph.id) % 100000},
    )


def _output_for(node, status: str) -> str:
    if status == "error":
        return f"{node.label.lower()} failed before producing output"
    configured = node.config.get("output")
    if isinstance(configured, str) and configured:
        return configured
    return _OUTPUTS.get(node.kind, f"{node.label.lower()} completed")


def load_example_flows(directory: str | Path | None = None) -> dict[str, FlowGraph]:
    """Load every flow-shaped JSON file from a directory (or one file)."""
    location = Path(directory) if directory is not None else DEFAULT_EXAMPLES
    if location.is_file():
        files = [location]
    elif location.is_dir():
        files = sorted(location.glob("*.json"))
    else:
        raise GraphError(f"examples path not found: {location}")

    flows: dict[str, FlowGraph] = {}
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, Mapping) or "nodes" not in data or "edges" not in data:
            continue
        graph = FlowGraph.from_dict(data)
        if graph.id in flows:
            raise GraphError(f"duplicate flow id: {graph.id}")
        flows[graph.id] = graph
    if not flows:
        raise GraphError(f"no flow JSON found in {location}")
    return flows


class Studio:
    """API facade independent of the HTTP transport."""

    def __init__(
        self,
        flows: Iterable[FlowGraph] = (),
        store: RunStore | None = None,
    ) -> None:
        self.flows: dict[str, FlowGraph] = {}
        self.store = store if store is not None else RunStore()
        for graph in flows:
            self.add_flow(graph)

    def add_flow(self, graph: FlowGraph) -> FlowGraph:
        if graph.id in self.flows:
            raise GraphError(f"duplicate flow id: {graph.id}")
        self.flows[graph.id] = graph
        return graph

    def default_flow_id(self) -> str:
        if not self.flows:
            raise GraphError("no flows are loaded")
        return next(iter(self.flows))

    def get_flow(self, flow_id: str) -> FlowGraph:
        try:
            return self.flows[flow_id]
        except KeyError:
            raise GraphError(f"unknown flow: {flow_id}") from None

    def flow_payloads(self) -> list[dict[str, Any]]:
        payloads = []
        for graph in self.flows.values():
            issues = graph.validate()
            item = {
                "id": graph.id,
                "name": graph.name,
                "node_count": graph.node_count,
                "edge_count": graph.edge_count,
                "valid": not issues,
                "issues": issues,
                "nodes": [node.to_dict() for node in graph.nodes.values()],
                "edges": [edge.to_dict() for edge in graph.edges],
                "order": graph.topological_order() if not issues else [],
            }
            payloads.append(item)
        return payloads

    def run_payloads(self) -> list[dict[str, Any]]:
        return [run.to_dict() for run in self.store.list_runs()]

    def create_run(
        self, flow_id: str | None = None, *, fail_node: str | None = None
    ) -> Run:
        graph = self.get_flow(flow_id or self.default_flow_id())
        run = simulate_run(graph, fail_node=fail_node)
        return self.store.add(run)

    def replay(self, run_id: str) -> Run:
        run = self.store.get(run_id)
        flow = self.flows.get(run.flow_id)
        return self.store.replay(run_id, flow=flow)


def seed_studio(
    *,
    examples: str | Path | None = None,
    data: str | Path | None = None,
) -> Studio:
    """Build a studio from example flows and a persistent (or new) store."""
    flows = load_example_flows(examples)
    store = RunStore(data if data is not None else DEFAULT_DATA)
    if len(store) == 0:
        location = Path(examples) if examples is not None else DEFAULT_EXAMPLES
        run_file = location / "run.json" if location.is_dir() else None
        if run_file is not None and run_file.exists():
            try:
                store.add(Run.load(run_file))
            except Exception:
                pass
    return Studio(flows=flows.values(), store=store)


class StudioHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        studio: Studio,
        *,
        static_dir: str | Path = STATIC_DIR,
        verbose: bool = False,
    ) -> None:
        self.studio = studio
        self.static_dir = Path(static_dir).resolve()
        self.verbose = verbose
        super().__init__(address, _StudioHandler)


class _StudioHandler(BaseHTTPRequestHandler):
    server: StudioHTTPServer
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- dispatch
    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        path = unquote(urlsplit(self.path).path)
        try:
            if path.startswith("/api/"):
                self._handle_api(method, path)
            elif method in {"GET", "HEAD"}:
                self._serve_static(path, head=method == "HEAD")
            else:
                self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"})
        except (GraphError, StoreError, KeyError, ValueError) as exc:
            message = exc.args[0] if exc.args else str(exc)
            status = HTTPStatus.NOT_FOUND if isinstance(exc, KeyError) else HTTPStatus.BAD_REQUEST
            self._json(status, {"error": str(message)})
        except BrokenPipeError:
            return
        except Exception:
            if self.server.verbose:
                traceback.print_exc()
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

    # ------------------------------------------------------------------ api
    def _handle_api(self, method: str, path: str) -> None:
        studio = self.server.studio
        if method == "GET":
            if path == "/api/health":
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "service": "observatory",
                        "version": _VERSION,
                        "flows": len(studio.flows),
                        "runs": len(studio.store),
                    },
                )
                return
            if path == "/api/flows":
                self._json(HTTPStatus.OK, {"flows": studio.flow_payloads()})
                return
            if path == "/api/runs":
                runs = studio.run_payloads()
                self._json(
                    HTTPStatus.OK,
                    {"runs": runs, "latest": runs[0]["id"] if runs else None},
                )
                return
            if path.startswith("/api/flows/"):
                graph = studio.get_flow(path[len("/api/flows/"):])
                payload = next(
                    item for item in studio.flow_payloads() if item["id"] == graph.id
                )
                self._json(HTTPStatus.OK, {"flow": payload})
                return
            if path.startswith("/api/runs/"):
                run = studio.store.get(path[len("/api/runs/"):])
                self._json(HTTPStatus.OK, {"run": run.to_dict()})
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            return

        if method == "POST":
            body = self._read_json()
            if path == "/api/runs":
                flow_id = body.get("flow_id")
                fail_node = body.get("fail_node")
                run = studio.create_run(flow_id, fail_node=fail_node)
                self._json(
                    HTTPStatus.CREATED,
                    {"run": run.to_dict(), "summary": summarize(run)},
                )
                return
            if path == "/api/replay":
                run_id = body.get("run_id")
                if not run_id:
                    raise ValueError("run_id is required")
                run = studio.replay(str(run_id))
                self._json(
                    HTTPStatus.CREATED,
                    {"run": run.to_dict(), "summary": summarize(run)},
                )
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            return

        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"})

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("invalid Content-Length") from None
        if length > 1_000_000:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("request body must be JSON") from None
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    # --------------------------------------------------------------- static
    def _serve_static(self, path: str, *, head: bool) -> None:
        relative = path.lstrip("/") or "index.html"
        if relative.startswith("static/"):
            relative = relative[len("static/"):]
        if relative.endswith("/"):
            relative += "index.html"
        root = self.server.static_dir
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            self._not_found()
            return
        if not target.is_file():
            if relative == "index.html":
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "web/index.html is missing"},
                )
            else:
                self._not_found()
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head:
            self.wfile.write(data)

    # ------------------------------------------------------------- responses
    def _json(self, status: HTTPStatus, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _not_found(self) -> None:
        body = b"not found\n"
        self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        if self.server.verbose:
            super().log_message(format, *args)


def create_server(
    studio: Studio,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    static_dir: str | Path = STATIC_DIR,
    verbose: bool = False,
) -> StudioHTTPServer:
    return StudioHTTPServer(
        (host, port), studio, static_dir=static_dir, verbose=verbose
    )


def serve(
    studio: Studio | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    static_dir: str | Path = STATIC_DIR,
    verbose: bool = False,
) -> None:
    studio = studio if studio is not None else seed_studio()
    httpd = create_server(
        studio, host=host, port=port, static_dir=static_dir, verbose=verbose
    )
    bound_host, bound_port = httpd.server_address[:2]
    print(f"Observatory listening on http://{bound_host}:{bound_port}")
    print(f"flows {len(studio.flows)} · runs {len(studio.store)} · static {static_dir}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
