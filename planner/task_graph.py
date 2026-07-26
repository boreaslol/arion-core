"""Versioned task-graph contract for the Arion local team runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Iterable

from planner.domain_pack import validate_domain_pack_identity


TASK_GRAPH_VERSION = "1.0"
ALLOWED_NODE_KINDS = {
    "semantic_resolution",
    "deterministic_tool",
    "role_runtime",
    "external_agent",
    "human_gate",
    "assurance",
}
ALLOWED_SIDE_EFFECT_CLASSES = {"none", "local_read", "local_write"}
FORBIDDEN_SIDE_EFFECT_CLASSES = {"remote_write", "production_write"}
SYSTEM_ASSIGNEES = {"System"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _tupled(values: Iterable[Any] | None) -> tuple[str, ...]:
    if not values:
        return ()
    return tuple(str(value) for value in values if str(value).strip())


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "task"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    backoff_seconds: int = 0

    def validate(self) -> list[str]:
        issues: list[str] = []
        if self.max_attempts < 1:
            issues.append("retry_policy.max_attempts must be at least 1")
        if self.backoff_seconds < 0:
            issues.append("retry_policy.backoff_seconds must not be negative")
        return issues

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "RetryPolicy":
        payload = payload or {}
        return cls(
            max_attempts=int(payload.get("max_attempts", 1)),
            backoff_seconds=int(payload.get("backoff_seconds", 0)),
        )


@dataclass(frozen=True)
class TaskNode:
    node_id: str
    title: str
    kind: str
    executor_id: str
    assignee_role: str
    depends_on: tuple[str, ...] = ()
    input_refs: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    evidence_requirements: tuple[str, ...] = ()
    acceptance_gates: tuple[str, ...] = ()
    side_effect_class: str = "none"
    idempotency_key: str = ""
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(
        self,
        *,
        governance_roles: Iterable[str] | None = None,
        orchestrator_role: str | None = None,
    ) -> list[str]:
        issues: list[str] = []
        governed_roles = {
            str(role) for role in governance_roles or () if str(role).strip()
        }
        execution_roles = governed_roles - {
            str(orchestrator_role or "").strip()
        }
        if not self.node_id.strip():
            issues.append("node_id is required")
        if not self.title.strip():
            issues.append(f"node[{self.node_id}].title is required")
        if self.kind not in ALLOWED_NODE_KINDS:
            issues.append(f"node[{self.node_id}].kind is unsupported: {self.kind}")
        if not self.executor_id.strip():
            issues.append(f"node[{self.node_id}].executor_id is required")
        if not self.assignee_role.strip():
            issues.append(
                f"node[{self.node_id}].assignee_role is required"
            )
        elif governed_roles and self.assignee_role not in (
            governed_roles | SYSTEM_ASSIGNEES
        ):
            issues.append(
                f"node[{self.node_id}].assignee_role is unsupported: "
                f"{self.assignee_role}"
            )
        if (
            self.kind == "role_runtime"
            and governed_roles
            and self.assignee_role not in execution_roles
        ):
            issues.append(
                f"node[{self.node_id}].role_runtime requires a governed specialist role assignee"
            )
        if self.kind == "external_agent":
            if governed_roles and self.assignee_role not in execution_roles:
                issues.append(
                    f"node[{self.node_id}].external_agent requires a governed specialist role assignee"
                )
            if not str(self.metadata.get("agent_instance_id") or "").strip():
                issues.append(
                    f"node[{self.node_id}].external_agent requires metadata.agent_instance_id"
                )
            if not str(self.metadata.get("task_contract_id") or "").strip():
                issues.append(
                    f"node[{self.node_id}].external_agent requires metadata.task_contract_id"
                )
        governance_role = str(self.metadata.get("governance_role") or "")
        if governance_role and governance_role != self.assignee_role:
            issues.append(
                f"node[{self.node_id}].metadata.governance_role must match assignee_role"
            )
        if self.side_effect_class in FORBIDDEN_SIDE_EFFECT_CLASSES:
            issues.append(
                f"node[{self.node_id}] requests forbidden side effect: {self.side_effect_class}"
            )
        elif self.side_effect_class not in ALLOWED_SIDE_EFFECT_CLASSES:
            issues.append(
                f"node[{self.node_id}].side_effect_class is unsupported: "
                f"{self.side_effect_class}"
            )
        if not self.idempotency_key.strip():
            issues.append(f"node[{self.node_id}].idempotency_key is required")
        issues.extend(
            f"node[{self.node_id}].{issue}" for issue in self.retry_policy.validate()
        )
        return issues

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskNode":
        return cls(
            node_id=str(payload.get("node_id", "")),
            title=str(payload.get("title", "")),
            kind=str(payload.get("kind", "")),
            executor_id=str(payload.get("executor_id", "")),
            assignee_role=str(payload.get("assignee_role", "")),
            depends_on=_tupled(payload.get("depends_on")),
            input_refs=_tupled(payload.get("input_refs")),
            expected_outputs=_tupled(payload.get("expected_outputs")),
            evidence_requirements=_tupled(payload.get("evidence_requirements")),
            acceptance_gates=_tupled(payload.get("acceptance_gates")),
            side_effect_class=str(payload.get("side_effect_class", "none")),
            idempotency_key=str(payload.get("idempotency_key", "")),
            retry_policy=RetryPolicy.from_dict(payload.get("retry_policy")),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True)
class TaskGraph:
    graph_id: str
    run_id: str
    objective: str
    planner_policy_version: str
    nodes: tuple[TaskNode, ...]
    semantic_snapshot_id: str | None = None
    version: str = TASK_GRAPH_VERSION
    created_at: str = field(default_factory=utc_now)
    boundaries: dict[str, Any] = field(
        default_factory=lambda: {
            "local_only": True,
            "production_apply_allowed": False,
            "remote_write_allowed": False,
            "secret_management_in_scope": False,
        }
    )
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        issues: list[str] = []
        governance_roles = tuple(
            str(role)
            for role in self.metadata.get("governance_roles", []) or []
            if str(role).strip()
        )
        orchestrator_role = str(
            self.metadata.get("orchestrator_role") or ""
        ).strip()
        has_governance_assignee = any(
            node.assignee_role not in SYSTEM_ASSIGNEES
            for node in self.nodes
        )
        if not governance_roles and has_governance_assignee:
            issues.append("task graph metadata.governance_roles is required")
        elif len(governance_roles) != len(set(governance_roles)):
            issues.append("task graph governance_roles must be unique")
        if not orchestrator_role and governance_roles:
            orchestrator_role = governance_roles[0]
        elif governance_roles and orchestrator_role not in governance_roles:
            issues.append("task graph orchestrator_role must be governed")
        if has_governance_assignee:
            issues.extend(
                validate_domain_pack_identity(self.metadata.get("domain_pack"))
            )
        elif self.metadata.get("domain_pack") is not None:
            issues.extend(
                validate_domain_pack_identity(self.metadata.get("domain_pack"))
            )
        if self.version != TASK_GRAPH_VERSION:
            issues.append(
                f"task graph version must be {TASK_GRAPH_VERSION}, got {self.version}"
            )
        if not self.graph_id.strip():
            issues.append("graph_id is required")
        if not self.run_id.strip():
            issues.append("run_id is required")
        if not self.objective.strip():
            issues.append("objective is required")
        if not self.planner_policy_version.strip():
            issues.append("planner_policy_version is required")
        if not self.nodes:
            issues.append("task graph must contain at least one node")

        node_ids = [node.node_id for node in self.nodes]
        duplicates = sorted({node_id for node_id in node_ids if node_ids.count(node_id) > 1})
        if duplicates:
            issues.append(f"task graph contains duplicate node ids: {', '.join(duplicates)}")
        node_id_set = set(node_ids)
        idempotency_keys = [node.idempotency_key for node in self.nodes]
        duplicate_keys = sorted(
            {key for key in idempotency_keys if key and idempotency_keys.count(key) > 1}
        )
        if duplicate_keys:
            issues.append(
                "task graph contains duplicate idempotency keys: " + ", ".join(duplicate_keys)
            )

        for node in self.nodes:
            issues.extend(
                node.validate(
                    governance_roles=governance_roles,
                    orchestrator_role=orchestrator_role,
                )
            )
            for dependency in node.depends_on:
                if dependency == node.node_id:
                    issues.append(f"node[{node.node_id}] cannot depend on itself")
                elif dependency not in node_id_set:
                    issues.append(
                        f"node[{node.node_id}] references missing dependency: {dependency}"
                    )

        if not issues:
            try:
                self.topological_order()
            except ValueError as exc:
                issues.append(str(exc))

        if not bool(self.boundaries.get("local_only")):
            issues.append("task graph boundary local_only must remain true")
        if bool(self.boundaries.get("production_apply_allowed")):
            issues.append("production apply is outside the runtime-v1 goal boundary")
        if bool(self.boundaries.get("remote_write_allowed")):
            issues.append("remote writes are outside the runtime-v1 goal boundary")
        return issues

    def require_valid(self) -> None:
        issues = self.validate()
        if issues:
            raise ValueError("invalid task graph: " + "; ".join(issues))

    def topological_order(self) -> tuple[str, ...]:
        dependencies = {
            node.node_id: set(node.depends_on)
            for node in self.nodes
        }
        order: list[str] = []
        while dependencies:
            ready = sorted(
                node_id for node_id, required in dependencies.items() if not required
            )
            if not ready:
                cycle_nodes = ", ".join(sorted(dependencies))
                raise ValueError(f"task graph contains a dependency cycle: {cycle_nodes}")
            order.extend(ready)
            for node_id in ready:
                dependencies.pop(node_id)
            for required in dependencies.values():
                required.difference_update(ready)
        return tuple(order)

    def node(self, node_id: str) -> TaskNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)

    def ready_nodes(self, statuses: dict[str, str]) -> tuple[TaskNode, ...]:
        completed = {
            node_id
            for node_id, status in statuses.items()
            if status in {"succeeded", "skipped"}
        }
        return tuple(
            self.node(node_id)
            for node_id in self.topological_order()
            if statuses.get(node_id, "pending") == "pending"
            and set(self.node(node_id).depends_on).issubset(completed)
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskGraph":
        return cls(
            version=str(payload.get("version", TASK_GRAPH_VERSION)),
            graph_id=str(payload.get("graph_id", "")),
            run_id=str(payload.get("run_id", "")),
            objective=str(payload.get("objective", "")),
            planner_policy_version=str(payload.get("planner_policy_version", "")),
            semantic_snapshot_id=payload.get("semantic_snapshot_id"),
            created_at=str(payload.get("created_at") or utc_now()),
            nodes=tuple(TaskNode.from_dict(item) for item in payload.get("nodes", [])),
            boundaries=dict(payload.get("boundaries") or {}),
            metadata=dict(payload.get("metadata") or {}),
        )


def compile_capability_plan_to_task_graph(
    *,
    run_id: str,
    capability_plan: dict[str, Any],
    governance_roles: tuple[str, ...],
    orchestrator_role: str,
    role_output_files: dict[str, str],
    domain_pack_identity: dict[str, str],
    planner_decision: dict[str, Any] | None = None,
    planner_policy_version: str = "arion-runtime-v1.3",
) -> TaskGraph:
    """Compile the current CapabilityPlan contract into a runtime-v1 DAG."""

    collaboration = dict(capability_plan.get("collaboration_plan") or {})
    governed_role_set = set(governance_roles)
    if not governance_roles:
        raise ValueError("task graph compilation requires governance roles")
    if governance_roles[0] != orchestrator_role:
        raise ValueError("orchestrator role must be first in governance role order")
    task_packages = [
        item for item in collaboration.get("task_packages", []) if isinstance(item, dict)
    ]
    planner_declares_active_roles = bool(
        planner_decision is not None and "active_roles" in planner_decision
    )
    requested_active_roles = {
        str(role)
        for role in (planner_decision or {}).get("active_roles", [])
        if str(role).strip()
    }
    all_task_package_roles = {
        str(item.get("assignee_role") or "") for item in task_packages
    }
    unsupported_task_roles = all_task_package_roles - (
        governed_role_set - {orchestrator_role}
    )
    if unsupported_task_roles:
        raise ValueError(
            "collaboration plan contains unsupported execution roles: "
            + ", ".join(sorted(unsupported_task_roles))
        )
    unknown_active_roles = requested_active_roles - all_task_package_roles
    if unknown_active_roles:
        raise ValueError(
            "planner decision activates roles absent from collaboration plan: "
            + ", ".join(sorted(unknown_active_roles))
        )
    active_task_packages = [
        item
        for item in task_packages
        if not planner_declares_active_roles
        or str(item.get("assignee_role") or "") in requested_active_roles
    ]
    activated_roles = {
        str(item.get("assignee_role") or "") for item in active_task_packages
    }
    primary_role = str(
        collaboration.get("primary_role")
        or next(iter(sorted(activated_roles)), orchestrator_role)
    )
    objective = str(capability_plan.get("question") or "Execute Arion capability plan")
    semantic_snapshot_id = None
    if planner_decision:
        semantic_snapshot_id = str(
            planner_decision.get("decision_id")
            or planner_decision.get("semantic_snapshot_id")
            or ""
        ) or None

    nodes: list[TaskNode] = [
        TaskNode(
            node_id="plan.semantic-context",
            title="Resolve semantic context and candidate routes",
            kind="semantic_resolution",
            executor_id="semantic_context_snapshot",
            assignee_role="System",
            expected_outputs=("planner_decision", "semantic_references"),
            evidence_requirements=("planner_policy_version", "route_candidates"),
            acceptance_gates=("semantic_contract_resolved",),
            side_effect_class="local_write",
            idempotency_key=f"{run_id}:plan.semantic-context",
            metadata={
                "planner_decision": planner_decision or {},
                "governed_owner_role": orchestrator_role,
            },
        )
    ]

    execution_target = dict(capability_plan.get("execution_target") or {})
    target_kind = str(execution_target.get("kind") or "route")
    target_id = str(
        execution_target.get("id") or capability_plan.get("route_family") or ""
    ).strip()
    primary_node_id: str | None = None
    if target_kind != "none":
        if not target_id:
            raise ValueError("execution target id is required")
        primary_node_id = "execute.primary"
        nodes.append(
            TaskNode(
                node_id=primary_node_id,
                title=f"Execute primary target {target_id}",
                kind="deterministic_tool",
                executor_id=f"{target_kind}:{target_id}",
                assignee_role="System",
                depends_on=("plan.semantic-context",),
                input_refs=_tupled(capability_plan.get("knowledge_packs")),
                expected_outputs=_tupled(capability_plan.get("expected_outputs")),
                evidence_requirements=("execution_target", "input_manifest"),
                acceptance_gates=("deterministic_execution",),
                side_effect_class="local_write",
                idempotency_key=f"{run_id}:{primary_node_id}",
                retry_policy=RetryPolicy(max_attempts=2),
                metadata={
                    "execution_target": execution_target,
                    "query_families": list(
                        capability_plan.get("query_families") or []
                    ),
                    "governed_owner_role": (
                        primary_role
                        if primary_role in governed_role_set
                        else orchestrator_role
                    ),
                },
            )
        )

    agent_tasks: list[tuple[int, dict[str, Any], str, str, str]] = []
    role_to_node_ids: dict[str, list[str]] = {}
    task_id_to_node_id: dict[str, str] = {}
    for index, task_package in enumerate(active_task_packages, start=1):
        role = str(task_package.get("assignee_role") or "")
        task_id = str(task_package.get("task_id") or f"{_slug(role)}-{index:02d}")
        if task_id in task_id_to_node_id:
            raise ValueError(f"duplicate active task package id: {task_id}")
        node_id = f"role.{index:02d}.{_slug(role)}"
        task_id_to_node_id[task_id] = node_id
        role_to_node_ids.setdefault(role, []).append(node_id)
        agent_tasks.append((index, task_package, role, task_id, node_id))

    parallel_handoffs = {
        str(parent): {str(target) for target in targets or []}
        for parent, targets in (collaboration.get("parallel_handoffs") or {}).items()
    }

    for index, task_package, role, task_id, node_id in agent_tasks:
        dependencies: list[str] = []
        explicit_task_dependencies = _tupled(task_package.get("depends_on_task_ids"))
        if explicit_task_dependencies:
            for dependency_task_id in explicit_task_dependencies:
                if dependency_task_id == "execute.primary":
                    if primary_node_id is None:
                        raise ValueError(
                            f"active task {task_id} requires an absent analysis target"
                        )
                    dependencies.append(primary_node_id)
                    continue
                dependency_node_id = task_id_to_node_id.get(dependency_task_id)
                if dependency_node_id is None:
                    raise ValueError(
                        f"active task {task_id} depends on unavailable task package: "
                        f"{dependency_task_id}"
                    )
                dependencies.append(dependency_node_id)
        elif role == primary_role:
            dependencies.append(primary_node_id or "plan.semantic-context")
        else:
            parallel_parent_nodes = [
                parent_node_id
                for parent, targets in parallel_handoffs.items()
                if role in targets
                for parent_node_id in role_to_node_ids.get(parent, ())
            ]
            if parallel_parent_nodes:
                dependencies.extend(parallel_parent_nodes)
            if not dependencies:
                dependencies.append(primary_node_id or "plan.semantic-context")

        nodes.append(
            TaskNode(
                node_id=node_id,
                title=str(task_package.get("objective") or f"Run {role} task"),
                kind="role_runtime",
                executor_id=f"role_runtime:{_slug(role)}",
                assignee_role=role,
                depends_on=tuple(dict.fromkeys(dependencies)),
                input_refs=_tupled(task_package.get("knowledge_files")),
                expected_outputs=_tupled(task_package.get("output_format")),
                evidence_requirements=_tupled(
                    (task_package.get("acceptance_contract") or {}).get(
                        "required_evidence"
                    )
                )
                or ("role_runtime_status",),
                acceptance_gates=_tupled(
                    (task_package.get("acceptance_contract") or {}).get("gates")
                ),
                side_effect_class="local_write",
                idempotency_key=f"{run_id}:{node_id}",
                retry_policy=RetryPolicy(max_attempts=2),
                metadata={
                    "task_package": task_package,
                    "agent_instance_id": f"agent:{run_id}:{node_id}",
                    "task_contract_id": task_id,
                    "governance_role": role,
                    "agent_runtime_kind": "governed_role_runtime",
                },
            )
        )

    terminal_dependencies = tuple(
        node.node_id
        for node in nodes
        if node.kind == "role_runtime"
    ) or ((primary_node_id,) if primary_node_id else ("plan.semantic-context",))
    assurance_node_id = "assurance.system"
    nodes.append(
        TaskNode(
            node_id=assurance_node_id,
            title="Run deterministic local runtime assurance",
            kind="assurance",
            executor_id="system_assurance",
            assignee_role="System",
            depends_on=terminal_dependencies,
            expected_outputs=("assurance_report", "residual_risks"),
            evidence_requirements=("task_graph", "runtime_events", "artifact_manifest"),
            acceptance_gates=("system_assurance",),
            side_effect_class="local_write",
            idempotency_key=f"{run_id}:{assurance_node_id}",
            metadata={
                "executor_class": "deterministic_system_gate",
                "governance_role": False,
                "independent_actor_claimed": False,
                "domain_acceptance_remains_separate": True,
            },
        )
    )
    nodes.append(
        TaskNode(
            node_id="gate.orchestrator-closure",
            title=(
                f"{orchestrator_role} reviews evidence and closes or redirects "
                "the run"
            ),
            kind="human_gate",
            executor_id="human_gate:orchestrator_closure",
            assignee_role=orchestrator_role,
            depends_on=(assurance_node_id,),
            expected_outputs=("orchestrator_decision",),
            evidence_requirements=("assurance_report", "unresolved_items"),
            acceptance_gates=("orchestrator_closure",),
            side_effect_class="local_write",
            idempotency_key=f"{run_id}:gate.orchestrator-closure",
            metadata={
                "auto_close_allowed": False,
                "closes_task": True,
                "orchestrator_role": orchestrator_role,
            },
        )
    )

    graph = TaskGraph(
        graph_id=f"task-graph:{run_id}",
        run_id=run_id,
        objective=objective,
        planner_policy_version=planner_policy_version,
        semantic_snapshot_id=semantic_snapshot_id,
        nodes=tuple(nodes),
        metadata={
            "capability_plan": capability_plan,
            "planner_decision": planner_decision or {},
            "standard_chain": collaboration.get("standard_chain"),
            "activation_mode": (
                "planner_selected"
                if planner_declares_active_roles
                else "contract_declared"
            ),
            "requested_active_roles": sorted(requested_active_roles),
            "active_roles": sorted(activated_roles),
            "agent_instance_count": len(agent_tasks),
            "agent_instances": [
                {
                    "agent_instance_id": f"agent:{run_id}:{node_id}",
                    "task_contract_id": task_id,
                    "governance_role": role,
                    "node_id": node_id,
                }
                for _, _, role, task_id, node_id in agent_tasks
            ],
            "deferred_role_candidates": (
                sorted(all_task_package_roles - activated_roles)
                if planner_declares_active_roles
                else []
            ),
            "governance_roles": list(governance_roles),
            "orchestrator_role": orchestrator_role,
            "role_output_files": dict(role_output_files),
            "domain_pack": dict(domain_pack_identity),
        },
    )
    graph.require_valid()
    return graph
