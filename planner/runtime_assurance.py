"""Domain-neutral deterministic assurance for one local TaskGraph run."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

from planner.runtime_outcome import normalize_runtime_outcome
from planner.task_graph import TaskGraph


FORBIDDEN_TRUE_OUTPUT_FLAGS = {
    "apply_performed",
    "production_apply_performed",
    "production_write_performed",
    "remote_command_performed",
    "remote_write_performed",
}


@dataclass(frozen=True)
class AssuranceFinding:
    lane: str
    status: str
    severity: str
    message: str
    evidence_refs: tuple[str, ...] = ()


def _node_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(node["node_id"]): node
        for node in snapshot.get("nodes", [])
    }


def run_system_assurance(
    *,
    graph: TaskGraph,
    runtime_snapshot: dict[str, Any],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    findings: list[AssuranceFinding] = []
    graph_issues = graph.validate()
    findings.append(
        AssuranceFinding(
            lane="runtime_contract",
            status="pass" if not graph_issues else "fail",
            severity="info" if not graph_issues else "error",
            message=(
                "TaskGraph contract is valid"
                if not graph_issues
                else "; ".join(graph_issues)
            ),
        )
    )
    if (
        not graph.boundaries.get("local_only")
        or graph.boundaries.get("production_apply_allowed")
    ):
        findings.append(
            AssuranceFinding(
                lane="runtime_boundary",
                status="fail",
                severity="error",
                message="runtime boundary is not local-only",
            )
        )

    nodes = _node_map(runtime_snapshot)
    incomplete = [
        node.node_id
        for node in graph.nodes
        if (
            node.kind not in {"assurance", "human_gate"}
            and nodes.get(node.node_id, {}).get("status")
            not in {"succeeded", "skipped"}
        )
    ]
    if incomplete:
        findings.append(
            AssuranceFinding(
                lane="runtime_completion",
                status="fail",
                severity="error",
                message=(
                    "assurance started before prerequisite nodes closed: "
                    + ", ".join(incomplete)
                ),
            )
        )

    for node in graph.nodes:
        if node.kind != "role_runtime":
            continue
        runtime_node = nodes.get(node.node_id, {})
        output = runtime_node.get("output") or {}
        forbidden = sorted(
            flag
            for flag in FORBIDDEN_TRUE_OUTPUT_FLAGS
            if output.get(flag) is True
        )
        findings.append(
            AssuranceFinding(
                lane="side_effect_boundary",
                status="pass" if not forbidden else "fail",
                severity="info" if not forbidden else "error",
                message=(
                    f"{node.node_id} preserves the local no-apply boundary"
                    if not forbidden
                    else (
                        f"{node.node_id} reported forbidden actions: "
                        + ", ".join(forbidden)
                    )
                ),
                evidence_refs=(node.node_id,),
            )
        )
        outcome = normalize_runtime_outcome(output)
        if (
            runtime_node.get("status") == "succeeded"
            and outcome["execution_status"] != "succeeded"
        ):
            findings.append(
                AssuranceFinding(
                    lane="runtime_outcome",
                    status="fail",
                    severity="error",
                    message=(
                        f"{node.node_id} succeeded while its normalized outcome "
                        f"is {outcome['execution_status']}: "
                        f"{outcome['source_status']}"
                    ),
                    evidence_refs=(node.node_id,),
                )
            )

    findings.append(
        AssuranceFinding(
            lane="actor_independence",
            status="deferred_scope",
            severity="info",
            message=(
                "node separation is not proof of real actor or permission "
                "independence"
            ),
        )
    )
    failures = [finding for finding in findings if finding.status == "fail"]
    warnings = [finding for finding in findings if finding.status == "warning"]
    return {
        "status": "pass" if not failures else "fail",
        "assurance_kind": "deterministic_system_gate",
        "governance_role": None,
        "independent_actor_claimed": False,
        "failure_count": len(failures),
        "warning_count": len(warnings),
        "domain_acceptance_complete": False,
        "orchestrator_closure_complete": False,
        "identity_permission_isolation_status": "deferred_external_scope",
        "findings": [asdict(finding) for finding in findings],
    }


run_independent_assurance = run_system_assurance


def write_assurance_report(
    path: str | Path,
    report: dict[str, Any],
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target
