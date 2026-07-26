"""Compile one bounded, run-local capability proposal from current contracts."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from typing import Any

import yaml

from planner.plan_models import (
    CapabilityPlan,
    CollaborationPlan,
    ExecutionTarget,
    TaskPackage,
    TimeWindow,
)
from planner.role_registry import GovernanceRegistry
from planner.route_contracts import RouteDecision


ROLE_CONTRACT_SCOPE = "run_local_question_profiles"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_") or "agent"


def _tupled(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    return tuple(str(item) for item in value if str(item).strip())


def _read_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"expected role contract mapping: {path}")
    return payload


@lru_cache(maxsize=4)
def load_role_contracts(
    path: str | Path,
    role_order: tuple[str, ...],
    registry_reference: str,
) -> dict[str, dict[str, Any]]:
    payload = _read_yaml(Path(path))
    if payload.get("canonical_role_registry") != registry_reference:
        raise ValueError(
            "role contracts must reference the canonical governance registry: "
            f"{registry_reference}"
        )
    if payload.get("contract_scope") != ROLE_CONTRACT_SCOPE:
        raise ValueError(
            "role contracts may only define run-local question profiles"
        )
    if payload.get("defines_governance_authority") is not False:
        raise ValueError(
            "role contracts must not define governance authority"
        )
    roles = {
        str(role): dict(contract or {})
        for role, contract in (payload.get("roles") or {}).items()
    }
    missing = set(role_order) - set(roles)
    if missing:
        raise ValueError(
            "role contracts are missing governed roles: "
            + ", ".join(sorted(missing))
        )
    return roles


def _merge_contract(
    contract: dict[str, Any],
    override: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(contract)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _task_id(role: str, route_family: str) -> str:
    return f"{_slug(role)}-{_slug(route_family)}"


def _dependency_map(
    active_roles: tuple[str, ...],
    requested: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    active = set(active_roles)
    dependencies: dict[str, tuple[str, ...]] = {}
    for role in active_roles:
        configured = tuple(requested.get(role, ()))
        if configured:
            unavailable = set(configured) - active
            if unavailable:
                raise ValueError(
                    f"{role} depends on inactive roles: "
                    + ", ".join(sorted(unavailable))
                )
            dependencies[role] = configured
        else:
            dependencies[role] = ()
    return dependencies


def build_capability_plan(
    *,
    question: str,
    route_family: str,
    route_decision: RouteDecision,
    start_date=None,
    end_date=None,
    requested_capability_id: str | None = None,
    capability_binding_mode: str | None = None,
    standard_chain_id: str | None = None,
    workflow_id: str | None = None,
    owner_role: str | None = None,
    active_roles: tuple[str, ...],
    governance: GovernanceRegistry,
    analysis_required: bool = True,
    agent_dependencies: dict[str, tuple[str, ...]] | None = None,
    role_overrides: dict[str, dict[str, Any]] | None = None,
    agent_assignments: tuple[dict[str, Any], ...] = (),
    role_contracts_path: str | Path,
    intent_family: str | None = None,
) -> CapabilityPlan:
    role_contracts = load_role_contracts(
        role_contracts_path,
        governance.role_order,
        governance.registry_reference,
    )
    active_roles = tuple(dict.fromkeys(str(role) for role in active_roles))
    if not active_roles:
        raise ValueError("a capability proposal requires at least one active role")
    unsupported = set(active_roles) - set(governance.execution_roles)
    if unsupported:
        raise ValueError(
            "unsupported active roles: " + ", ".join(sorted(unsupported))
        )
    primary_role = (
        str(owner_role)
        if owner_role in active_roles
        else active_roles[0]
    )
    dependencies = _dependency_map(
        active_roles,
        dict(agent_dependencies or {}),
    )
    dependents: dict[str, list[str]] = {role: [] for role in active_roles}
    for role, upstream_roles in dependencies.items():
        for upstream_role in upstream_roles:
            dependents[upstream_role].append(role)

    explicit_assignments = [dict(item) for item in agent_assignments]
    assignment_ids = [
        str(item.get("assignment_id") or "").strip()
        for item in explicit_assignments
    ]
    if any(not assignment_id for assignment_id in assignment_ids):
        raise ValueError("agent assignments require assignment_id")
    if len(assignment_ids) != len(set(assignment_ids)):
        raise ValueError("agent assignment ids must be unique")
    unsupported_assignment_roles = {
        str(item.get("role") or "")
        for item in explicit_assignments
    } - set(active_roles)
    if unsupported_assignment_roles:
        raise ValueError(
            "agent assignments target inactive roles: "
            + ", ".join(sorted(unsupported_assignment_roles))
        )

    assignments_by_role: dict[str, list[dict[str, Any]]] = {
        role: [] for role in active_roles
    }
    for assignment in explicit_assignments:
        assignments_by_role[str(assignment["role"])].append(assignment)
    for role in active_roles:
        if assignments_by_role[role]:
            continue
        assignments_by_role[role].append(
            {
                "assignment_id": f"default:{_slug(role)}",
                "role": role,
                "source_field": "workflow_contract",
                "statement": "",
                "depends_on_assignment_ids": [],
                "default_assignment": True,
            }
        )

    task_id_by_assignment: dict[str, str] = {}
    role_task_ids: dict[str, list[str]] = {
        role: [] for role in active_roles
    }
    for role in active_roles:
        for assignment in assignments_by_role[role]:
            assignment_id = str(assignment["assignment_id"])
            task_id = _task_id(role, route_family)
            if not assignment.get("default_assignment"):
                task_id += f"-{_slug(assignment_id)}"
            if task_id in task_id_by_assignment.values():
                raise ValueError(f"duplicate compiled task id: {task_id}")
            task_id_by_assignment[assignment_id] = task_id
            role_task_ids[role].append(task_id)

    task_packages: list[TaskPackage] = []
    for role in active_roles:
        contract = _merge_contract(
            role_contracts[role],
            (role_overrides or {}).get(role),
        )
        next_owners = tuple(
            dict.fromkeys([*dependents[role], governance.orchestrator_role])
        )
        known_facts = [
            f"当前问题已选择 route_family={route_family}。",
            f"本轮由 {role} 承担其不可约问题责任。",
        ]
        if workflow_id:
            known_facts.append(f"当前 workflow_id={workflow_id}。")
        if route_decision.matched_signals:
            known_facts.append(
                "当前匹配信号: "
                + ", ".join(route_decision.matched_signals)
                + "。"
            )
        for assignment in assignments_by_role[role]:
            assignment_id = str(assignment["assignment_id"])
            statement = str(assignment.get("statement") or "").strip()
            explicit_dependencies = tuple(
                str(value)
                for value in assignment.get(
                    "depends_on_assignment_ids"
                )
                or ()
            )
            if explicit_dependencies:
                missing_dependencies = (
                    set(explicit_dependencies) - set(task_id_by_assignment)
                )
                if missing_dependencies:
                    raise ValueError(
                        f"agent assignment {assignment_id} depends on "
                        "unknown assignments: "
                        + ", ".join(sorted(missing_dependencies))
                    )
                task_dependencies = tuple(
                    task_id_by_assignment[value]
                    for value in explicit_dependencies
                )
            else:
                task_dependencies = tuple(
                    task_id
                    for dependency_role in dependencies[role]
                    for task_id in role_task_ids[dependency_role]
                )
            task_known_facts = [
                *known_facts,
                f"当前 Agent assignment_id={assignment_id}。",
            ]
            task_packages.append(
                TaskPackage(
                    assignee_role=role,
                    task_id=task_id_by_assignment[assignment_id],
                    objective=(
                        f"{str(contract.get('objective') or '').strip()} "
                        + (
                            f"开放问题：{statement}"
                            if statement
                            else f"当前问题：{question}"
                        )
                    ).strip(),
                    depends_on_task_ids=task_dependencies,
                    current_context=tuple(
                        item
                        for item in (
                            f"question={question}",
                            f"route_family={route_family}",
                            (
                                f"workflow_id={workflow_id}"
                                if workflow_id
                                else ""
                            ),
                            (
                                "working_set_source="
                                f"{assignment.get('source_field')}"
                                if assignment.get("source_field")
                                else ""
                            ),
                            (
                                "time_window="
                                f"{start_date.isoformat() if start_date else 'default'}"
                                "->"
                                f"{end_date.isoformat() if end_date else 'default'}"
                            ),
                        )
                        if item
                    ),
                    known_facts=tuple(task_known_facts),
                    hypotheses=_tupled(contract.get("hypotheses")),
                    questions_to_answer=(
                        (statement,)
                        if statement
                        else _tupled(contract.get("questions"))
                    ),
                    scope_boundary=_tupled(contract.get("boundaries")),
                    output_format=_tupled(contract.get("outputs")),
                    next_possible_handoff=next_owners,
                    knowledge_files=_tupled(
                        contract.get("knowledge_files")
                    ),
                    stop_point=str(contract.get("stop_point") or ""),
                    handoff_reason=(
                        "This Agent instance owns one explicit open item "
                        f"from {assignment.get('source_field')}."
                        if statement
                        else (
                            f"{role} is active because its irreducible "
                            "question remains necessary for the current "
                            "judgment."
                        )
                    ),
                    acceptance_contract=dict(
                        contract.get("acceptance_contract") or {}
                    ),
                    domain_context={
                        "implementation_owner": str(
                            contract.get("implementation_owner")
                            or "not_required"
                        ),
                        "engineering_components": list(
                            _tupled(
                                contract.get("engineering_components")
                            )
                        ),
                        "interface_contracts": list(
                            _tupled(contract.get("interface_contracts"))
                        ),
                        "composition_version_contract": dict(
                            contract.get(
                                "composition_version_contract"
                            )
                            or {}
                        ),
                        "failure_semantics": list(
                            _tupled(contract.get("failure_semantics"))
                        ),
                        "execution_brief": dict(
                            contract.get("execution_brief") or {}
                        ),
                        "release_control": dict(
                            contract.get("release_control") or {}
                        ),
                    },
                )
            )

    parallel_handoffs = {
        role: tuple(children)
        for role, children in dependents.items()
        if len(children) > 1
    }
    chain_id = standard_chain_id or (
        f"{workflow_id}_run" if workflow_id else "direct_inquiry"
    )
    collaboration = CollaborationPlan(
        mode="dynamic_agent_assignments",
        lead_role=governance.orchestrator_role,
        primary_role=primary_role,
        standard_chain=chain_id,
        parallel_handoffs=parallel_handoffs,
        task_packages=tuple(task_packages),
    )
    execution_target = ExecutionTarget(
        kind="route" if analysis_required else "none",
        id=route_family if analysis_required else (workflow_id or route_family),
    )
    requested_skills = (
        (requested_capability_id,)
        if requested_capability_id
        else ()
    )
    return CapabilityPlan(
        question=question,
        intent_family=str(intent_family or route_decision.intent_family),
        route_family=route_family,
        requested_capability_id=requested_capability_id,
        capability_binding_mode=capability_binding_mode,
        entities=route_decision.detected_entities,
        dimensions=route_decision.detected_dimensions,
        time_window=TimeWindow(
            start_date=start_date.isoformat() if start_date else None,
            end_date=end_date.isoformat() if end_date else None,
        ),
        skill_chain=requested_skills,
        guardrails=tuple(
            dict.fromkeys(
                [
                    *route_decision.required_preconditions,
                    *route_decision.next_actions,
                ]
            )
        ),
        query_families=route_decision.query_families if analysis_required else (),
        execution_target=execution_target,
        collaboration_plan=collaboration,
        expected_outputs=(
            "runtime_outcome",
            "artifact_manifest",
            "working_set_revision",
        ),
        reasoning_summary=(
            f"route={route_family}; workflow={workflow_id or 'bounded_fallback'}; "
            f"roles={','.join(active_roles)}; agents={len(task_packages)}; "
            f"mode={collaboration.mode}"
        ),
    )
