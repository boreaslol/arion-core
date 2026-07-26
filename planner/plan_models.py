#!/usr/bin/env python3
"""Plan models for the capability planner."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class TimeWindow:
    start_date: str | None = None
    end_date: str | None = None


@dataclass(frozen=True)
class ExecutionTarget:
    kind: str
    id: str


@dataclass(frozen=True)
class TaskPackage:
    assignee_role: str
    task_id: str
    objective: str
    depends_on_task_ids: tuple[str, ...] = ()
    current_context: tuple[str, ...] = ()
    known_facts: tuple[str, ...] = ()
    hypotheses: tuple[str, ...] = ()
    questions_to_answer: tuple[str, ...] = ()
    scope_boundary: tuple[str, ...] = ()
    output_format: tuple[str, ...] = ()
    next_possible_handoff: tuple[str, ...] = ()
    knowledge_files: tuple[str, ...] = ()
    resolved_scope: tuple[str, ...] = ()
    unresolved_residual: tuple[str, ...] = ()
    stop_point: str = ""
    handoff_reason: str = ""
    acceptance_contract: dict[str, Any] = field(default_factory=dict)
    domain_context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CollaborationPlan:
    mode: str = "dynamic_agent_assignments"
    lead_role: str = ""
    primary_role: str = ""
    standard_chain: str = "direct_inquiry"
    parallel_handoffs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    task_packages: tuple[TaskPackage, ...] = ()


@dataclass(frozen=True)
class CapabilityPlan:
    question: str
    intent_family: str
    route_family: str
    requested_capability_id: str | None = None
    capability_binding_mode: str | None = None
    entities: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    time_window: TimeWindow = field(default_factory=TimeWindow)
    comparison_mode: str | None = None
    knowledge_packs: tuple[str, ...] = ()
    skill_chain: tuple[str, ...] = ()
    guardrails: tuple[str, ...] = ()
    query_families: tuple[str, ...] = ()
    execution_target: ExecutionTarget = field(
        default_factory=lambda: ExecutionTarget(kind="none", id="")
    )
    collaboration_plan: CollaborationPlan = field(default_factory=CollaborationPlan)
    expected_outputs: tuple[str, ...] = ()
    reasoning_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
