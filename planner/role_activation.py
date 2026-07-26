"""Role-pool admission and per-run activation artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from planner.task_graph import TaskGraph


ROLE_ACTIVATION_MANIFEST_VERSION = "1.0"


def _ordered_roles(
    values: Iterable[str],
    *,
    governance_roles: tuple[str, ...],
) -> list[str]:
    requested = {
        str(value) for value in values if str(value) in governance_roles
    }
    return [role for role in governance_roles if role in requested]


def _build_manifest(
    *,
    run_id: str,
    graph_id: str | None,
    planner_policy_version: str,
    activation_mode: str,
    execution_model: str,
    activated_role_refs: Mapping[str, Iterable[str]],
    agent_instances: Iterable[Mapping[str, Any]],
    deferred_role_candidates: Iterable[str],
    system_nodes: Iterable[Mapping[str, Any]],
    governance_roles: tuple[str, ...],
    orchestrator_role: str,
    role_output_files: Mapping[str, str],
    created_at: str,
    phase: str,
) -> dict[str, Any]:
    activated_roles = _ordered_roles(
        activated_role_refs,
        governance_roles=governance_roles,
    )
    normalized_agent_instances = [dict(item) for item in agent_instances]
    deferred_roles = _ordered_roles(
        deferred_role_candidates,
        governance_roles=governance_roles,
    )
    participating_roles = _ordered_roles(
        [orchestrator_role, *activated_roles],
        governance_roles=governance_roles,
    )
    role_assignments: list[dict[str, Any]] = []
    for role in governance_roles:
        if role == orchestrator_role:
            status = "orchestrator"
            activation_reason = "persistent_orchestrator"
        elif role in activated_roles:
            status = "activated"
            activation_reason = activation_mode
        elif role in deferred_roles:
            status = "deferred_candidate"
            activation_reason = "candidate_not_selected_for_execution"
        else:
            status = "not_applicable"
            activation_reason = "outside_selected_workflow"
        role_assignments.append(
            {
                "role": role,
                "activation_status": status,
                "activation_reason": activation_reason,
                "execution_refs": list(activated_role_refs.get(role, ())),
                "agent_instance_ids": [
                    str(item.get("agent_instance_id"))
                    for item in normalized_agent_instances
                    if item.get("governance_role") == role
                ],
                "output_file": role_output_files[role],
                "output_required": role == orchestrator_role
                or status == "activated",
            }
        )

    return {
        "version": ROLE_ACTIVATION_MANIFEST_VERSION,
        "run_id": run_id,
        "graph_id": graph_id,
        "planner_policy_version": planner_policy_version,
        "created_at": created_at,
        "phase": phase,
        "activation_mode": activation_mode,
        "governance_model": "fixed_role_registry",
        "execution_model": execution_model,
        "governance_roles": list(governance_roles),
        "orchestrator_roles": [orchestrator_role],
        "activated_agent_roles": activated_roles,
        "agent_instances": normalized_agent_instances,
        "participating_governance_roles": participating_roles,
        "deferred_agent_roles": deferred_roles,
        "role_assignments": role_assignments,
        "system_nodes": [dict(node) for node in system_nodes],
        "contract": {
            "role_outputs_are_lazy": True,
            "governance_roles_and_agent_instances_are_separate": True,
            "multiple_agent_instances_per_role_allowed": True,
            "inactive_role_outputs_required": False,
            "system_nodes_are_governance_roles": False,
            "system_assurance_claims_actor_independence": False,
            "actor_independence_requires_recorded_identity": True,
        },
    }


def build_task_graph_role_activation_manifest(graph: TaskGraph) -> dict[str, Any]:
    governance_roles = tuple(
        str(role)
        for role in graph.metadata.get("governance_roles", []) or []
        if str(role).strip()
    )
    orchestrator_role = str(
        graph.metadata.get("orchestrator_role") or ""
    ).strip()
    role_output_files = {
        str(role): str(path)
        for role, path in (
            graph.metadata.get("role_output_files") or {}
        ).items()
    }
    if not governance_roles or orchestrator_role not in governance_roles:
        raise ValueError("TaskGraph is missing its Domain Pack role contract")
    missing_output_roles = set(governance_roles) - set(role_output_files)
    if missing_output_roles:
        raise ValueError(
            "TaskGraph is missing role output files: "
            + ", ".join(sorted(missing_output_roles))
        )
    activated_role_refs: dict[str, list[str]] = {}
    agent_instances: list[dict[str, Any]] = []
    system_nodes: list[dict[str, Any]] = []
    for node in graph.nodes:
        if node.kind == "role_runtime":
            activated_role_refs.setdefault(node.assignee_role, []).append(node.node_id)
            agent_instances.append(
                {
                    "agent_instance_id": str(
                        node.metadata.get("agent_instance_id") or node.node_id
                    ),
                    "task_contract_id": str(
                        node.metadata.get("task_contract_id") or node.node_id
                    ),
                    "governance_role": node.assignee_role,
                    "node_id": node.node_id,
                    "executor_id": node.executor_id,
                }
            )
        if node.assignee_role == "System":
            system_nodes.append(
                {
                    "node_id": node.node_id,
                    "kind": node.kind,
                    "executor_id": node.executor_id,
                    "governed_owner_role": node.metadata.get("governed_owner_role"),
                }
            )
    return _build_manifest(
        run_id=graph.run_id,
        graph_id=graph.graph_id,
        planner_policy_version=graph.planner_policy_version,
        activation_mode=str(graph.metadata.get("activation_mode") or "task_graph"),
        execution_model="dynamic_task_graph",
        activated_role_refs=activated_role_refs,
        agent_instances=agent_instances,
        deferred_role_candidates=graph.metadata.get("deferred_role_candidates") or (),
        system_nodes=system_nodes,
        governance_roles=governance_roles,
        orchestrator_role=orchestrator_role,
        role_output_files=role_output_files,
        created_at=graph.created_at,
        phase="planned",
    )


def write_role_activation_manifest(
    path: str | Path,
    payload: Mapping[str, Any],
    *,
    require_same_if_exists: bool = False,
) -> Path:
    target = Path(path)
    normalized = dict(payload)
    if target.exists() and require_same_if_exists:
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing != normalized:
            raise ValueError(
                "run directory already contains a different role activation manifest: "
                f"{target}"
            )
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target
