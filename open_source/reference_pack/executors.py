"""Synthetic executors that demonstrate the Domain Pack boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from planner.unified_execution import ExecutionResult


def _execution_identity(context: Any) -> tuple[str, str]:
    plan = getattr(context, "plan", None)
    if plan is not None:
        return str(plan.executor_id), str(plan.governance_role)
    node = context.node
    return str(node.executor_id), str(node.assignee_role)


def _write_synthetic_artifact(
    context: Any,
    *,
    name: str,
    payload: dict[str, Any],
) -> Path:
    path = context.run_dir / "artifacts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def bounded_inquiry_executor(context: Any) -> ExecutionResult:
    executor_id, governance_role = _execution_identity(context)
    artifact = _write_synthetic_artifact(
        context,
        name="synthetic_observation.json",
        payload={
            "observation": "The synthetic signal changed within the bounded sample.",
            "interpretation": "The cause remains unresolved.",
            "counterfactual": "The signal would remain stable under an alternative cause.",
            "revision_trigger": "A second independent observation contradicts the shift.",
        },
    )
    return ExecutionResult(
        output={
            "status": "synthetic_observation_completed",
            "executor_id": executor_id,
            "governance_role": governance_role,
            "working_set_patch": {
                "append": {
                    "knowns": [
                        {
                            "kind": "synthetic_observation",
                            "statement": (
                                "The bounded synthetic signal changed; cause is unresolved."
                            ),
                        }
                    ],
                    "counterfactuals": [
                        {
                            "kind": "alternative_explanation",
                            "statement": (
                                "The change could disappear under an alternative cause."
                            ),
                        }
                    ],
                    "revision_triggers": [
                        {
                            "kind": "new_observation",
                            "condition": (
                                "A second independent observation contradicts the shift."
                            ),
                        }
                    ],
                }
            },
        },
        artifacts=(
            {"kind": "synthetic_observation", "path": str(artifact)},
        ),
    )


def investigator_executor(context: Any) -> ExecutionResult:
    artifact = _write_synthetic_artifact(
        context,
        name=f"{context.node.node_id}.json",
        payload={
            "status": "bounded",
            "role": context.node.assignee_role,
            "finding": "Observed experience and interpretation remain separate.",
        },
    )
    return ExecutionResult(
        output={
            "status": "synthetic_investigation_completed",
            "working_set_patch": {
                "append": {
                    "knowns": [
                        {
                            "kind": "evidence_boundary",
                            "source_role": context.node.assignee_role,
                            "statement": (
                                "Observed experience and interpretation remain separate."
                            ),
                        }
                    ]
                }
            },
        },
        artifacts=(
            {"kind": "synthetic_role_output", "path": str(artifact)},
        ),
    )
