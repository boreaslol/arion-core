"""Local TaskGraph execution engine with idempotent resume semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import traceback
from typing import TYPE_CHECKING, Any, Callable

from planner.runtime_event_store import ClaimedNode, RuntimeEventStore
from planner.task_graph import TaskGraph, TaskNode

if TYPE_CHECKING:
    from planner.domain_pack import DomainPack


@dataclass(frozen=True)
class ExecutionResult:
    status: str = "succeeded"
    output: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[dict[str, Any], ...] = ()
    retryable: bool = True
    wait_reason: str = ""


@dataclass(frozen=True)
class ExecutionContext:
    run_id: str
    graph: TaskGraph
    node: TaskNode
    attempt: int
    run_dir: Path
    store: RuntimeEventStore


Executor = Callable[[Any], ExecutionResult | dict[str, Any] | None]


class ExecutorRegistry:
    def __init__(
        self,
        *,
        domain_pack_identity: dict[str, str] | None = None,
    ) -> None:
        self._exact: dict[str, Executor] = {}
        self._prefix: list[tuple[str, Executor]] = []
        self.domain_pack_identity = (
            dict(domain_pack_identity)
            if domain_pack_identity is not None
            else None
        )

    def register(self, executor_id: str, executor: Executor) -> None:
        if not executor_id.strip():
            raise ValueError("executor_id is required")
        self._exact[executor_id] = executor

    def register_prefix(self, prefix: str, executor: Executor) -> None:
        if not prefix.strip():
            raise ValueError("executor prefix is required")
        self._prefix = [(key, value) for key, value in self._prefix if key != prefix]
        self._prefix.append((prefix, executor))
        self._prefix.sort(key=lambda item: len(item[0]), reverse=True)

    def resolve(self, executor_id: str) -> Executor:
        if executor_id in self._exact:
            return self._exact[executor_id]
        for prefix, executor in self._prefix:
            if executor_id.startswith(prefix):
                return executor
        raise KeyError(f"no executor registered for {executor_id}")

    def require_compatible_domain_pack(self, value: Any) -> None:
        if self.domain_pack_identity is None:
            if value is not None:
                raise ValueError(
                    "executor registry is not bound to the execution plan "
                    "Domain Pack"
                )
            return
        if not isinstance(value, dict) or value != self.domain_pack_identity:
            raise ValueError(
                "executor registry Domain Pack does not match execution plan"
            )


def normalize_execution_result(
    value: ExecutionResult | dict[str, Any] | None,
) -> ExecutionResult:
    if value is None:
        return ExecutionResult()
    if isinstance(value, ExecutionResult):
        return value
    if isinstance(value, dict):
        return ExecutionResult(output=value)
    raise TypeError(f"executor returned unsupported result: {type(value).__name__}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_run_artifacts(
    artifacts: tuple[dict[str, Any], ...],
    run_dir: Path,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    resolved_run_dir = run_dir.resolve()
    for artifact in artifacts:
        if "path" not in artifact:
            raise ValueError("artifact path is required")
        path = Path(str(artifact["path"]))
        if not path.is_absolute():
            path = run_dir / path
        resolved = path.resolve()
        if not resolved.is_relative_to(resolved_run_dir):
            raise ValueError(f"artifact escapes local run directory: {resolved}")
        if not resolved.is_file():
            raise ValueError(f"artifact does not exist: {resolved}")
        normalized.append(
            {
                "kind": str(artifact.get("kind") or "file"),
                "path": str(resolved),
                "sha256": str(artifact.get("sha256") or sha256_file(resolved)),
                "metadata": dict(artifact.get("metadata") or {}),
            }
        )
    return normalized


class UnifiedExecutionEngine:
    """Execute one local DAG until completion, wait, failure, or idle."""

    def __init__(
        self,
        *,
        store: RuntimeEventStore,
        registry: ExecutorRegistry,
        run_root: str | Path,
        worker_id: str = "local-worker",
        lease_seconds: int = 300,
    ) -> None:
        self.store = store
        self.registry = registry
        self.run_root = Path(run_root)
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    def register(self, graph: TaskGraph) -> Path:
        from planner.role_activation import (
            build_task_graph_role_activation_manifest,
            write_role_activation_manifest,
        )

        graph.require_valid()
        self.registry.require_compatible_domain_pack(
            graph.metadata.get("domain_pack")
        )
        run_dir = self.run_root / graph.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        graph_path = run_dir / "task_graph.json"
        if graph_path.exists():
            existing = json.loads(graph_path.read_text(encoding="utf-8"))
            if json.dumps(existing, sort_keys=True) != json.dumps(
                graph.to_dict(), sort_keys=True
            ):
                raise ValueError(f"run directory already contains a different graph: {run_dir}")
        else:
            graph_path.write_text(
                json.dumps(graph.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        write_role_activation_manifest(
            run_dir / "facts" / "role_activation_manifest.json",
            build_task_graph_role_activation_manifest(graph),
            require_same_if_exists=True,
        )
        self.store.register_graph(graph)
        return run_dir

    def run(self, graph: TaskGraph, *, max_nodes: int | None = None) -> dict[str, Any]:
        run_dir = self.register(graph)
        self.store.recover_expired_leases()
        executed = 0
        while max_nodes is None or executed < max_nodes:
            claim = self.store.claim_ready_node(
                graph.run_id,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
            if claim is None:
                break
            self._execute_claim(claim, graph=graph, run_dir=run_dir)
            executed += 1
            snapshot = self.store.run_snapshot(graph.run_id)
            if snapshot["status"] in {"failed", "succeeded", "cancelled"}:
                break
        self.store.export_canonical_views(graph.run_id, run_dir)
        return self.store.run_snapshot(graph.run_id)

    def resume(self, run_id: str, *, max_nodes: int | None = None) -> dict[str, Any]:
        return self.run(self.store.get_graph(run_id), max_nodes=max_nodes)

    def _execute_claim(self, claim: ClaimedNode, *, graph: TaskGraph, run_dir: Path) -> None:
        node = claim.node
        if node.side_effect_class not in {"none", "local_read", "local_write"}:
            self.store.fail_node(
                claim,
                error={"type": "forbidden_side_effect", "side_effect_class": node.side_effect_class},
                retryable=False,
            )
            return
        try:
            executor = self.registry.resolve(node.executor_id)
            result = normalize_execution_result(
                executor(
                    ExecutionContext(
                        run_id=graph.run_id,
                        graph=graph,
                        node=node,
                        attempt=claim.attempt,
                        run_dir=run_dir,
                        store=self.store,
                    )
                )
            )
            if result.status == "succeeded":
                artifacts = validate_run_artifacts(result.artifacts, run_dir)
                self.store.complete_node(claim, output=result.output, artifacts=artifacts)
            elif result.status in {"waiting_human", "waiting_external"}:
                artifacts = validate_run_artifacts(result.artifacts, run_dir)
                self.store.wait_node(
                    claim,
                    reason=result.wait_reason or result.status,
                    waiting_status=result.status,
                    payload=result.output,
                    artifacts=artifacts,
                )
            elif result.status == "failed":
                artifacts = validate_run_artifacts(result.artifacts, run_dir)
                self.store.fail_node(
                    claim,
                    error=result.output or {"type": "executor_reported_failure"},
                    retryable=result.retryable,
                    artifacts=artifacts,
                )
            else:
                raise ValueError(f"executor returned unsupported status: {result.status}")
        except Exception as exc:
            self.store.fail_node(
                claim,
                error={
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
                retryable=True,
            )


def semantic_snapshot_executor(context: ExecutionContext) -> ExecutionResult:
    payload = dict(context.node.metadata.get("planner_decision") or {})
    path = context.run_dir / "planner_decision.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return ExecutionResult(
        output={
            "decision_id": payload.get("decision_id"),
            "selected_route": payload.get("selected_route"),
        },
        artifacts=({"kind": "planner_decision", "path": str(path)},),
    )


def human_gate_executor(context: ExecutionContext) -> ExecutionResult:
    return ExecutionResult(
        status="waiting_human",
        output={
            "gate": context.node.executor_id,
            "assignee_role": context.node.assignee_role,
            "required_evidence": list(context.node.evidence_requirements),
        },
        wait_reason="explicit human decision is required",
    )


def _safe_output_name(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.casefold()).strip("-") or "agent"


def external_agent_request_executor(context: ExecutionContext) -> ExecutionResult:
    task_package = dict(context.node.metadata.get("task_package") or {})
    request = {
        "run_id": context.run_id,
        "node_id": context.node.node_id,
        "agent_instance_id": context.node.metadata.get("agent_instance_id"),
        "task_contract_id": context.node.metadata.get("task_contract_id"),
        "governance_role": context.node.assignee_role,
        "executor_id": context.node.executor_id,
        "task_package": task_package,
        "input_refs": list(context.node.input_refs),
        "expected_outputs": list(context.node.expected_outputs),
        "evidence_requirements": list(context.node.evidence_requirements),
    }
    request_path = (
        context.run_dir
        / "external_agent_requests"
        / f"{_safe_output_name(context.node.node_id)}.json"
    )
    request_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(request, ensure_ascii=False, indent=2) + "\n"
    if request_path.exists() and request_path.read_text(encoding="utf-8") != serialized:
        raise ValueError(f"external agent request changed after creation: {request_path}")
    request_path.write_text(serialized, encoding="utf-8")
    return ExecutionResult(
        status="waiting_external",
        output={
            "status": "external_agent_requested",
            "agent_instance_id": request["agent_instance_id"],
            "task_contract_id": request["task_contract_id"],
            "governance_role": request["governance_role"],
            "request_path": str(request_path),
        },
        artifacts=({"kind": "external_agent_request", "path": str(request_path)},),
        wait_reason="external agent result must be returned through the runtime gate",
    )


def system_assurance_executor(context: ExecutionContext) -> ExecutionResult:
    from planner.runtime_assurance import run_system_assurance, write_assurance_report

    report = run_system_assurance(
        graph=context.graph,
        runtime_snapshot=context.store.run_snapshot(context.run_id),
        artifacts=context.store.artifacts(context.run_id),
    )
    report_path = write_assurance_report(context.run_dir / "assurance_report.json", report)
    if report["status"] != "pass":
        return ExecutionResult(
            status="failed",
            output=report,
            artifacts=({"kind": "assurance_report", "path": str(report_path)},),
            retryable=False,
        )
    return ExecutionResult(
        output=report,
        artifacts=({"kind": "assurance_report", "path": str(report_path)},),
    )


independent_assurance_executor = system_assurance_executor


def build_task_graph_executor_registry(
    *,
    domain_pack: "DomainPack",
) -> ExecutorRegistry:
    from planner.executor_bindings import build_executor_registry

    return build_executor_registry(
        domain_pack,
        runtime_kind="task_graph",
    )
