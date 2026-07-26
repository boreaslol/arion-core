"""Minimal execution plan for bounded Arion work that does not need a DAG."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from planner.domain_pack import validate_domain_pack_identity
from planner.task_graph import (
    ALLOWED_SIDE_EFFECT_CLASSES,
    FORBIDDEN_SIDE_EFFECT_CLASSES,
)


DIRECT_RUN_VERSION = "1.0"
DIRECT_RUN_SIDE_EFFECT_CLASSES = {"none", "local_read"}


def _tupled(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value if str(item).strip())


@dataclass(frozen=True)
class DirectRunPlan:
    plan_id: str
    run_id: str
    objective: str
    planner_policy_version: str
    executor_id: str
    governance_role: str
    agent_instance_id: str
    task_contract_id: str
    input_refs: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    evidence_requirements: tuple[str, ...] = ()
    side_effect_class: str = "local_read"
    semantic_snapshot_id: str | None = None
    version: str = DIRECT_RUN_VERSION
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
        governance_roles = {
            str(role)
            for role in self.metadata.get("governance_roles", []) or []
            if str(role).strip()
        }
        orchestrator_role = str(
            self.metadata.get("orchestrator_role") or ""
        ).strip()
        if self.version != DIRECT_RUN_VERSION:
            issues.append(
                f"direct run version must be {DIRECT_RUN_VERSION}, got {self.version}"
            )
        if not self.plan_id.strip():
            issues.append("plan_id is required")
        if not self.run_id.strip():
            issues.append("run_id is required")
        if not self.objective.strip():
            issues.append("objective is required")
        if not self.planner_policy_version.strip():
            issues.append("planner_policy_version is required")
        if not self.executor_id.strip():
            issues.append("executor_id is required")
        issues.extend(validate_domain_pack_identity(self.metadata.get("domain_pack")))
        if not governance_roles:
            issues.append("direct run metadata.governance_roles is required")
        elif not orchestrator_role:
            issues.append("direct run metadata.orchestrator_role is required")
        elif self.governance_role not in (
            governance_roles - {orchestrator_role}
        ):
            issues.append(
                f"governance_role must be a governed specialist role: {self.governance_role}"
            )
        if not self.agent_instance_id.strip():
            issues.append("agent_instance_id is required")
        if not self.task_contract_id.strip():
            issues.append("task_contract_id is required")
        if self.side_effect_class in FORBIDDEN_SIDE_EFFECT_CLASSES:
            issues.append(
                f"direct run requests forbidden side effect: {self.side_effect_class}"
            )
        elif self.side_effect_class not in ALLOWED_SIDE_EFFECT_CLASSES:
            issues.append(
                f"direct run side_effect_class is unsupported: {self.side_effect_class}"
            )
        elif self.side_effect_class not in DIRECT_RUN_SIDE_EFFECT_CLASSES:
            issues.append(
                "direct run side_effect_class must be none or local_read: "
                f"{self.side_effect_class}"
            )
        if not bool(self.boundaries.get("local_only")):
            issues.append("direct run boundary local_only must remain true")
        if bool(self.boundaries.get("production_apply_allowed")):
            issues.append("direct run cannot permit production apply")
        if bool(self.boundaries.get("remote_write_allowed")):
            issues.append("direct run cannot permit remote writes")
        return issues

    def require_valid(self) -> None:
        issues = self.validate()
        if issues:
            raise ValueError("invalid direct run plan: " + "; ".join(issues))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compile_capability_plan_to_direct_run(
    *,
    run_id: str,
    capability_plan: dict[str, Any],
    planner_decision: dict[str, Any],
    planner_policy_version: str,
    governance_roles: tuple[str, ...],
    orchestrator_role: str,
    domain_pack_identity: dict[str, str],
) -> DirectRunPlan:
    active_roles = tuple(
        str(role)
        for role in planner_decision.get("active_roles", []) or []
        if str(role).strip()
    )
    if len(active_roles) != 1:
        raise ValueError(
            "direct run requires exactly one active governance role; "
            f"got {list(active_roles)}"
        )
    governance_role = active_roles[0]
    if governance_role not in (set(governance_roles) - {orchestrator_role}):
        raise ValueError(
            f"direct run role is not governed by the selected Domain Pack: "
            f"{governance_role}"
        )
    collaboration = dict(capability_plan.get("collaboration_plan") or {})
    task_packages = [
        item
        for item in collaboration.get("task_packages", []) or []
        if isinstance(item, dict)
        and str(item.get("assignee_role") or "") == governance_role
    ]
    if len(task_packages) != 1:
        raise ValueError(
            "direct run requires exactly one matching task package for "
            f"{governance_role}; got {len(task_packages)}"
        )
    task_package = task_packages[0]
    task_contract_id = str(task_package.get("task_id") or "").strip()
    execution_target = dict(capability_plan.get("execution_target") or {})
    target_kind = str(execution_target.get("kind") or "route")
    if target_kind == "none":
        raise ValueError("direct run requires an executable bounded target")
    target_id = str(
        execution_target.get("id")
        or capability_plan.get("route_family")
        or ""
    ).strip()
    if not target_id:
        raise ValueError("direct run execution target id is required")
    semantic_snapshot_id = str(
        planner_decision.get("decision_id")
        or planner_decision.get("semantic_snapshot_id")
        or ""
    ) or None
    plan = DirectRunPlan(
        plan_id=f"direct-run:{run_id}",
        run_id=run_id,
        objective=str(
            task_package.get("objective")
            or capability_plan.get("question")
            or "Execute Arion direct run"
        ),
        planner_policy_version=planner_policy_version,
        executor_id=f"{target_kind}:{target_id}",
        governance_role=governance_role,
        agent_instance_id=f"agent:{run_id}:direct",
        task_contract_id=task_contract_id,
        input_refs=tuple(
            dict.fromkeys(
                [
                    *_tupled(capability_plan.get("knowledge_packs")),
                    *_tupled(task_package.get("knowledge_files")),
                ]
            )
        ),
        expected_outputs=tuple(
            dict.fromkeys(
                [
                    *_tupled(capability_plan.get("expected_outputs")),
                    *_tupled(task_package.get("output_format")),
                ]
            )
        ),
        evidence_requirements=tuple(
            dict.fromkeys(
                [
                    "planner_decision",
                    "input_manifest",
                    *_tupled(
                        (task_package.get("acceptance_contract") or {}).get(
                            "required_evidence"
                        )
                    ),
                ]
            )
        ),
        semantic_snapshot_id=semantic_snapshot_id,
        metadata={
            "capability_plan": capability_plan,
            "execution_target": execution_target,
            "query_families": list(capability_plan.get("query_families") or []),
            "closure_policy": "owner_accountable_lead_on_exception",
            "task_package": task_package,
            "planner_decision": planner_decision,
            "domain_pack": dict(domain_pack_identity),
            "governance_roles": list(governance_roles),
            "orchestrator_role": orchestrator_role,
        },
    )
    plan.require_valid()
    return plan
