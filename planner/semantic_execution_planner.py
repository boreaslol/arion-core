"""Contract-first, explainable planner for the Arion local runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
from pathlib import Path
from typing import Any

import yaml

from planner.capability_planner import build_capability_plan
from planner.direct_run import DirectRunPlan, compile_capability_plan_to_direct_run
from planner.domain_pack import DomainPack
from planner.intent_utils import normalize_text
from planner.plan_models import CapabilityPlan
from planner.route_contracts import (
    RouteDecision,
    build_route_decision,
    load_route_contracts,
)
from planner.task_graph import (
    TaskGraph,
    compile_capability_plan_to_task_graph,
)
from planner.working_set_semantics import latest_runtime_handoff_batch


WORKING_SET_CLOSED_STATUSES = {
    "accepted",
    "closed",
    "dismissed",
    "done",
    "rejected",
    "resolved",
}


@dataclass(frozen=True)
class RouteCandidate:
    candidate_id: str
    route_family: str
    chain_id: str | None
    workflow_id: str | None
    requested_capability_id: str | None
    owner_role: str | None
    active_roles: tuple[str, ...]
    orchestration_mode: str
    score: float
    sources: tuple[str, ...]
    reasons: tuple[str, ...]
    maturity: str | None = None
    analysis_required: bool = True
    agent_dependencies: dict[str, tuple[str, ...]] | None = None
    role_overrides: dict[str, dict[str, Any]] | None = None
    working_set_reference: dict[str, Any] | None = None
    agent_assignments: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlannerDecision:
    decision_id: str
    policy_version: str
    question: str
    candidate_routes: tuple[RouteCandidate, ...]
    selected_route: str
    selected_chain: str | None
    selected_workflow_id: str | None
    selected_capability_id: str | None
    selected_owner_role: str | None
    active_roles: tuple[str, ...]
    orchestration_mode: str
    selection_reasons: tuple[str, ...]
    confidence: float
    alternatives: tuple[dict[str, Any], ...]
    semantic_references: tuple[dict[str, Any], ...]
    unresolved_items: tuple[str, ...]
    route_baseline: dict[str, Any]
    analysis_required: bool
    agent_dependencies: dict[str, tuple[str, ...]]
    role_overrides: dict[str, dict[str, Any]]
    agent_assignments: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["candidate_routes"] = [item.to_dict() for item in self.candidate_routes]
        return payload


@dataclass(frozen=True)
class PlannedRuntime:
    decision: PlannerDecision
    capability_plan: CapabilityPlan
    orchestration_mode: str
    direct_run: DirectRunPlan | None = None
    task_graph: TaskGraph | None = None


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"expected mapping in {path}")
    return payload


def _binding_candidate(
    *,
    binding: dict[str, Any],
    normalized_question: str,
) -> RouteCandidate | None:
    workflow_id = str(binding.get("workflow_id") or "")
    terms = [
        *(str(value) for value in binding.get("match_any", []) or []),
        *(str(value) for value in binding.get("aliases", []) or []),
        *(str(value) for value in binding.get("intent_tags", []) or []),
    ]
    matched_terms = [term for term in terms if normalize_text(term) in normalized_question]
    if not matched_terms:
        return None
    sources = ["workflow_contract", "runtime_policy_binding"]
    reasons = ["matched governed terms: " + ", ".join(matched_terms[:5])]
    score = min(0.96, 0.82 + 0.04 * len(set(matched_terms)))
    return RouteCandidate(
        candidate_id=f"workflow:{workflow_id}",
        route_family=str(binding["route_family"]),
        chain_id=str(binding.get("chain_id") or "") or None,
        workflow_id=workflow_id,
        requested_capability_id=str(binding.get("requested_capability_id") or "") or None,
        owner_role=str(binding.get("owner_role") or "") or None,
        active_roles=tuple(str(value) for value in binding.get("active_roles", []) or []),
        orchestration_mode=str(binding.get("orchestration_mode") or "task_graph"),
        score=round(score, 4),
        sources=tuple(sources),
        reasons=tuple(reasons),
        maturity=str(binding.get("maturity") or "active"),
        analysis_required=bool(binding.get("analysis_required", True)),
        agent_dependencies={
            str(role): tuple(str(value) for value in dependencies or [])
            for role, dependencies in (binding.get("agent_dependencies") or {}).items()
        },
        role_overrides={
            str(role): dict(value or {})
            for role, value in (binding.get("role_overrides") or {}).items()
        },
    )


def _working_set_role_signals(
    working_set: dict[str, Any] | None,
    *,
    governance_roles: tuple[str, ...],
    orchestrator_role: str,
) -> tuple[dict[str, Any], ...]:
    if not working_set:
        return ()
    allowed_roles = set(governance_roles) - {orchestrator_role}
    signals: list[dict[str, Any]] = []

    def collect(
        field_name: str,
        role_fields: tuple[str, ...],
        score: float,
    ) -> None:
        raw_items = working_set.get(field_name)
        items = [raw_items] if isinstance(raw_items, dict) else list(raw_items or [])
        for position, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").strip().casefold()
            if status in WORKING_SET_CLOSED_STATUSES:
                continue
            role_field = next(
                (
                    candidate
                    for candidate in role_fields
                    if str(item.get(candidate) or "").strip()
                    in allowed_roles
                ),
                None,
            )
            if role_field is None:
                continue
            role = str(item[role_field]).strip()
            statement = str(
                item.get("statement")
                or item.get("condition")
                or item.get("protected_boundary")
                or item.get("hypothesis")
                or item.get("reason")
                or ""
            ).strip()
            selector = {
                key: item[key]
                for key in (
                    "item_id",
                    "kind",
                    "statement",
                    "condition",
                    "run_id",
                    "node_id",
                )
                if item.get(key) is not None
            }
            signals.append(
                {
                    "role": role,
                    "source_field": field_name,
                    "source_role_field": role_field,
                    "score": score,
                    "statement": statement,
                    "working_set_reference": {
                        "field": field_name,
                        "position": position,
                        "selector": selector,
                    },
                }
            )

    sources = (
        (
            "active_obligations",
            ("required_role", "assignee_role", "owner_role", "governance_role"),
            0.99,
        ),
        (
            "current_judgment",
            ("required_next_owner", "owner_role", "governance_role"),
            0.98,
        ),
        (
            "unknowns",
            ("required_next_owner", "owner_role", "source_role"),
            0.97,
        ),
    )
    for field_name, role_fields, score in sources:
        collect(field_name, role_fields, score)

    raw_moves = working_set.get("next_candidate_moves")
    moves = [raw_moves] if isinstance(raw_moves, dict) else list(raw_moves or [])
    batch = latest_runtime_handoff_batch(moves)
    if batch:
        open_batch = [
            item
            for item in batch
            if str(item.get("status") or "open").strip().casefold() == "open"
        ]
        required = [
            item
            for item in open_batch
            if item.get("handoff_strength") == "required"
        ]
        recommended = [
            item
            for item in open_batch
            if item.get("handoff_strength") == "recommended"
        ]
        selected_moves = required if required else recommended
        handoff_score = 0.96 if required else 0.94
        for item in sorted(
            selected_moves,
            key=lambda value: int(value.get("handoff_rank") or 1),
        ):
            role = str(item.get("to_role") or "").strip()
            if role not in allowed_roles:
                continue
            signals.append(
                {
                    "role": role,
                    "source_field": "next_candidate_moves",
                    "source_role_field": "to_role",
                    "score": handoff_score,
                    "statement": str(item.get("reason") or "").strip(),
                    "working_set_reference": {
                        "field": "next_candidate_moves",
                        "selector": {
                            key: item[key]
                            for key in (
                                "kind",
                                "handoff_batch_id",
                                "to_role",
                                "handoff_strength",
                                "handoff_rank",
                            )
                            if item.get(key) is not None
                        },
                    },
                }
            )

    collect(
        "counterfactuals",
        ("required_next_owner", "owner_role", "source_role"),
        0.95,
    )
    if not signals:
        return ()
    highest_score = max(float(signal["score"]) for signal in signals)
    return tuple(
        signal
        for signal in signals
        if float(signal["score"]) == highest_score
    )


def _working_set_candidate(
    *,
    working_set: dict[str, Any] | None,
    bindings: list[dict[str, Any]],
    baseline: RouteDecision,
    governance_roles: tuple[str, ...],
    orchestrator_role: str,
    baseline_owner_role: str,
) -> tuple[RouteCandidate | None, str | None]:
    signals = _working_set_role_signals(
        working_set,
        governance_roles=governance_roles,
        orchestrator_role=orchestrator_role,
    )
    if not signals:
        return None, None
    required_roles = tuple(
        dict.fromkeys(str(signal["role"]) for signal in signals)
    )
    agent_assignments: list[dict[str, Any]] = []
    for signal in signals:
        assignment_identity = "|".join(
            (
                str((working_set or {}).get("task_id") or ""),
                str((working_set or {}).get("version") or ""),
                str(signal["source_field"]),
                str(signal["role"]),
                str(signal["statement"]),
                str(
                    (signal.get("working_set_reference") or {}).get(
                        "position"
                    )
                ),
            )
        )
        agent_assignments.append(
            {
                "assignment_id": (
                    "assignment-"
                    + hashlib.sha256(
                        assignment_identity.encode("utf-8")
                    ).hexdigest()[:16]
                ),
                "role": str(signal["role"]),
                "source_field": str(signal["source_field"]),
                "statement": str(signal["statement"]),
                "working_set_reference": dict(
                    signal.get("working_set_reference") or {}
                ),
                "depends_on_assignment_ids": [],
            }
        )
    identity = "|".join(
        [
            str((working_set or {}).get("task_id") or ""),
            str((working_set or {}).get("version") or ""),
            *(assignment["assignment_id"] for assignment in agent_assignments),
        ]
    )
    candidate_id = (
        "working-set:"
        + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    )
    reasons = [
        (
            "current Working Set assigns "
            f"{len(agent_assignments)} highest-priority open item(s) "
            "to governed Agent instances"
        )
    ]
    reasons.extend(
        f"open item: {signal['statement']}"
        for signal in signals
        if signal["statement"]
    )

    if set(required_roles) == {baseline_owner_role}:
        return (
            RouteCandidate(
                candidate_id=candidate_id,
                route_family=baseline.route_id,
                chain_id=None,
                workflow_id=None,
                requested_capability_id=None,
                owner_role=baseline_owner_role,
                active_roles=(baseline_owner_role,),
                orchestration_mode=(
                    "direct_run"
                    if len(agent_assignments) == 1
                    else "task_graph"
                ),
                score=float(signals[0]["score"]),
                sources=("current_working_set", "bounded_route_heuristic"),
                reasons=tuple(reasons),
                maturity="validated_runtime",
                analysis_required=True,
                agent_dependencies={},
                role_overrides={},
                working_set_reference=None,
                agent_assignments=tuple(agent_assignments),
            ),
            None,
        )

    required_role_set = set(required_roles)
    compatible = [
        binding
        for binding in bindings
        if required_role_set.issubset({
            str(value)
            for value in binding.get("active_roles", []) or []
        })
    ]
    if not compatible:
        return (
            None,
            (
                f"current Working Set requires {orchestrator_role} review because "
                "no validated "
                "runtime binding can execute the open responsibilities for "
                + ", ".join(required_roles)
            ),
        )
    compatible.sort(
        key=lambda binding: (
            len(binding.get("active_roles", []) or []),
            str(binding.get("workflow_id") or ""),
        )
    )
    binding = compatible[0]
    analysis_required = bool(binding.get("analysis_required", True))
    binding_roles = tuple(
        str(value) for value in binding.get("active_roles", []) or []
    )
    active_roles = required_roles if analysis_required else binding_roles
    if not analysis_required and len(active_roles) > 1:
        reasons.append(
            "action workflow retains its governed independent verification roles"
        )
    dependencies = {
        role_name: tuple(
            str(dependency)
            for dependency in raw_dependencies or []
            if str(dependency) in active_roles
        )
        for role_name, raw_dependencies in (
            binding.get("agent_dependencies") or {}
        ).items()
        if str(role_name) in active_roles
    }
    raw_overrides = dict(binding.get("role_overrides") or {})
    role_overrides = {
        role_name: dict(raw_overrides[role_name] or {})
        for role_name in active_roles
        if role_name in raw_overrides
    }
    return (
        RouteCandidate(
            candidate_id=candidate_id,
            route_family=str(binding["route_family"]),
            chain_id=str(binding.get("chain_id") or "") or None,
            workflow_id=str(binding.get("workflow_id") or "") or None,
            requested_capability_id=(
                str(binding.get("requested_capability_id") or "") or None
            ),
            owner_role=required_roles[0],
            active_roles=active_roles,
            orchestration_mode=str(
                binding.get("orchestration_mode") or "task_graph"
            ),
            score=float(signals[0]["score"]),
            sources=("current_working_set", "runtime_policy_binding"),
            reasons=tuple(reasons),
            maturity=str(binding.get("maturity") or "active"),
            analysis_required=analysis_required,
            agent_dependencies=dependencies,
            role_overrides=role_overrides,
            working_set_reference=next(
                (
                    signal.get("working_set_reference")
                    for signal in signals
                    if signal["source_field"] == "next_candidate_moves"
                ),
                None,
            ),
            agent_assignments=tuple(agent_assignments),
        ),
        None,
    )


def build_planner_decision(
    question: str,
    *,
    domain_pack: DomainPack,
    working_set: dict[str, Any] | None = None,
) -> tuple[PlannerDecision, RouteDecision]:
    policy_path = domain_pack.path("runtime_policy")
    route_contracts_path = domain_pack.path("route_contracts")
    policy = _read_yaml(policy_path)
    planning_policy = dict(policy.get("planning") or {})
    policy_version = str(policy.get("version") or "")
    if not policy_version:
        raise ValueError("runtime policy version is required")
    baseline_owner_role = str(
        planning_policy.get("baseline_owner_role") or ""
    ).strip()
    if baseline_owner_role not in domain_pack.governance.execution_roles:
        raise ValueError(
            "runtime policy planning.baseline_owner_role must be a governed "
            "execution role"
        )
    baseline = build_route_decision(question, path=route_contracts_path)
    unresolved: list[str] = []
    normalized_question = normalize_text(question)
    bindings = [
        binding
        for binding in planning_policy.get("workflow_bindings", []) or []
        if isinstance(binding, dict)
    ]
    candidates = [
        candidate
        for binding in bindings
        for candidate in [
            _binding_candidate(
                binding=binding,
                normalized_question=normalized_question,
            )
        ]
        if candidate is not None
    ]
    candidates.append(
        RouteCandidate(
            candidate_id=f"route:{baseline.route_id}",
            route_family=baseline.route_id,
            chain_id=None,
            workflow_id=None,
            requested_capability_id=None,
            owner_role=baseline_owner_role,
            active_roles=(baseline_owner_role,),
            orchestration_mode="direct_run",
            score=round(float(baseline.confidence), 4),
            sources=("bounded_route_heuristic",),
            reasons=(
                "matched route signals: " + ", ".join(baseline.matched_signals),
            ),
            maturity="validated_runtime",
            analysis_required=True,
            agent_dependencies={},
            role_overrides={},
        )
    )
    working_set_candidate, working_set_issue = _working_set_candidate(
        working_set=working_set,
        bindings=bindings,
        baseline=baseline,
        governance_roles=domain_pack.governance.role_order,
        orchestrator_role=domain_pack.governance.orchestrator_role,
        baseline_owner_role=baseline_owner_role,
    )
    if working_set_candidate is not None:
        candidates.append(working_set_candidate)
    if working_set_issue:
        unresolved.append(working_set_issue)
    candidates.sort(key=lambda item: (-item.score, item.candidate_id))
    selected = candidates[0]
    alternatives = tuple(
        {
            "candidate_id": item.candidate_id,
            "route_family": item.route_family,
            "chain_id": item.chain_id,
            "orchestration_mode": item.orchestration_mode,
            "score": item.score,
        }
        for item in candidates[1:]
    )
    if len(candidates) > 1:
        second = candidates[1]
        if (
            second.route_family != selected.route_family
            and selected.score - second.score < 0.08
        ):
            unresolved.append(
                "route ambiguity requires orchestrator review before "
                "high-impact action: "
                f"{selected.candidate_id} vs {second.candidate_id}"
            )
    digest_payload = "|".join(
        [
            policy_version,
            normalized_question,
            selected.candidate_id,
            selected.orchestration_mode,
            str(selected.score),
        ]
    )
    decision_id = "planner-" + hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()[:16]
    semantic_references: list[dict[str, Any]] = []
    if "current_working_set" in selected.sources:
        semantic_references.append(
            {
                "artifact_id": "current_working_set",
                "task_id": (working_set or {}).get("task_id"),
                "version": (working_set or {}).get("version"),
            }
        )
    if selected.working_set_reference:
        semantic_references.append(
            {
                "artifact_id": "working_set_handoff",
                **selected.working_set_reference,
            }
        )
    if selected.workflow_id:
        semantic_references.append(
            {
                "artifact_id": "runtime_workflow_contract",
                "contract_path": str(domain_pack.manifest["runtime_policy"]),
                "workflow_id": selected.workflow_id,
            }
        )
    else:
        semantic_references.append(
            {
                "artifact_id": "runtime_route_contract",
                "contract_path": str(domain_pack.manifest["route_contracts"]),
                "route_id": selected.route_family,
            }
        )
    semantic_references.append(
        {
            "artifact_id": "domain_pack",
            **domain_pack.identity,
        }
    )
    decision = PlannerDecision(
        decision_id=decision_id,
        policy_version=policy_version,
        question=question,
        candidate_routes=tuple(candidates),
        selected_route=selected.route_family,
        selected_chain=selected.chain_id,
        selected_workflow_id=selected.workflow_id,
        selected_capability_id=selected.requested_capability_id,
        selected_owner_role=selected.owner_role,
        active_roles=selected.active_roles,
        orchestration_mode=selected.orchestration_mode,
        selection_reasons=selected.reasons,
        confidence=selected.score,
        alternatives=alternatives,
        semantic_references=tuple(semantic_references),
        unresolved_items=tuple(unresolved),
        route_baseline=asdict(baseline),
        analysis_required=selected.analysis_required,
        agent_dependencies=dict(selected.agent_dependencies or {}),
        role_overrides=dict(selected.role_overrides or {}),
        agent_assignments=selected.agent_assignments,
    )
    return decision, baseline


def plan_runtime(
    *,
    domain_pack: DomainPack,
    question: str,
    run_id: str,
    working_set: dict[str, Any] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> PlannedRuntime:
    decision, baseline = build_planner_decision(
        question,
        domain_pack=domain_pack,
        working_set=working_set,
    )
    plan = build_capability_plan(
        question=question,
        route_family=decision.selected_route,
        route_decision=baseline,
        start_date=start_date,
        end_date=end_date,
        requested_capability_id=decision.selected_capability_id,
        capability_binding_mode="hard" if decision.selected_capability_id else None,
        standard_chain_id=decision.selected_chain,
        workflow_id=decision.selected_workflow_id,
        owner_role=decision.selected_owner_role,
        active_roles=decision.active_roles,
        governance=domain_pack.governance,
        analysis_required=decision.analysis_required,
        agent_dependencies=decision.agent_dependencies,
        role_overrides=decision.role_overrides,
        agent_assignments=decision.agent_assignments,
        role_contracts_path=domain_pack.path("role_contracts"),
        intent_family=load_route_contracts(
            domain_pack.path("route_contracts")
        )[decision.selected_route].intent_family,
    )
    if decision.orchestration_mode == "direct_run":
        direct_run = compile_capability_plan_to_direct_run(
            run_id=run_id,
            capability_plan=plan.to_dict(),
            planner_decision=decision.to_dict(),
            planner_policy_version=decision.policy_version,
            governance_roles=domain_pack.governance.role_order,
            orchestrator_role=domain_pack.governance.orchestrator_role,
            domain_pack_identity=domain_pack.identity,
        )
        return PlannedRuntime(
            decision=decision,
            capability_plan=plan,
            orchestration_mode=decision.orchestration_mode,
            direct_run=direct_run,
        )
    if decision.orchestration_mode != "task_graph":
        raise ValueError(
            f"unsupported orchestration mode: {decision.orchestration_mode}"
        )
    graph = compile_capability_plan_to_task_graph(
        run_id=run_id,
        capability_plan=plan.to_dict(),
        governance_roles=domain_pack.governance.role_order,
        orchestrator_role=domain_pack.governance.orchestrator_role,
        role_output_files=domain_pack.governance.role_output_files,
        domain_pack_identity=domain_pack.identity,
        planner_decision=decision.to_dict(),
        planner_policy_version=decision.policy_version,
    )
    return PlannedRuntime(
        decision=decision,
        capability_plan=plan,
        orchestration_mode=decision.orchestration_mode,
        task_graph=graph,
    )


def evaluate_planner_cases(
    cases: list[dict[str, Any]],
    *,
    run_id_prefix: str = "planner-shadow",
    **planner_kwargs: Any,
) -> dict[str, Any]:
    domain_pack = planner_kwargs.get("domain_pack")
    if not isinstance(domain_pack, DomainPack):
        raise ValueError("planner case evaluation requires an explicit Domain Pack")
    orchestrator_role = domain_pack.governance.orchestrator_role
    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        planned = plan_runtime(
            question=str(case["question"]),
            run_id=f"{run_id_prefix}-{index:03d}",
            **planner_kwargs,
        )
        expected_route = case.get("expected_route_family")
        expected_chain = case.get("expected_chain_id")
        expected_roles = {
            str(role)
            for role in case.get("expected_roles", []) or []
            if str(role) != orchestrator_role
        }
        expected_orchestration_mode = case.get("expected_orchestration_mode")
        actual_roles = set(planned.decision.active_roles)
        execution_plan = planned.direct_run or planned.task_graph
        if execution_plan is None:
            raise ValueError("planned runtime did not produce an execution plan")
        checks = {
            "route": not expected_route or planned.decision.selected_route == expected_route,
            "chain": not expected_chain
            or planned.capability_plan.collaboration_plan.standard_chain == expected_chain,
            "orchestration": not expected_orchestration_mode
            or planned.orchestration_mode == expected_orchestration_mode,
            "execution_plan": not execution_plan.validate(),
            "local_only": bool(execution_plan.boundaries.get("local_only")),
            "active_roles": not expected_roles or actual_roles == expected_roles,
        }
        results.append(
            {
                "case_id": case.get("id") or f"case-{index}",
                "decision_id": planned.decision.decision_id,
                "selected_route": planned.decision.selected_route,
                "selected_chain": planned.capability_plan.collaboration_plan.standard_chain,
                "selected_workflow_id": planned.decision.selected_workflow_id,
                "orchestration_mode": planned.orchestration_mode,
                "active_roles": sorted(actual_roles),
                "checks": checks,
                "passed": all(checks.values()),
            }
        )
    return {
        "case_count": len(results),
        "passed_count": sum(1 for result in results if result["passed"]),
        "failed_count": sum(1 for result in results if not result["passed"]),
        "promotion_allowed": False,
        "results": results,
    }


def evaluate_golden_workflows(
    *,
    domain_pack: DomainPack,
    **planner_kwargs: Any,
) -> dict[str, Any]:
    payload = _read_yaml(domain_pack.golden_workflows_path)
    return evaluate_planner_cases(
        list(payload.get("workflows") or []),
        domain_pack=domain_pack,
        **planner_kwargs,
    )
