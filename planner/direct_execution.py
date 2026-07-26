"""Execute one bounded DirectRunPlan without graph state or output mirrors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Any

from planner.direct_run import DirectRunPlan
from planner.unified_execution import (
    ExecutionResult,
    ExecutorRegistry,
    normalize_execution_result,
    validate_run_artifacts,
)
from planner.working_set_semantics import (
    derive_execution_working_set_append,
    finalize_runtime_handoff_claim,
    parse_executor_working_set_patch,
    resolve_working_set_fields,
)
from planner.working_set_store import (
    TaskRecord,
    WorkingSetSnapshot,
    WorkingSetStore,
)

if TYPE_CHECKING:
    from planner.domain_pack import DomainPack


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(serialized)
        handle.flush()
        temp_path = Path(handle.name)
    temp_path.replace(path)
    return path


def _write_json_if_same(path: Path, payload: dict[str, Any]) -> Path:
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if json.dumps(existing, sort_keys=True) != json.dumps(payload, sort_keys=True):
            raise ValueError(f"run directory contains a different artifact: {path}")
        return path
    return _atomic_write_json(path, payload)


@dataclass(frozen=True)
class DirectExecutionContext:
    run_id: str
    plan: DirectRunPlan
    run_dir: Path
    task: TaskRecord
    working_set: WorkingSetSnapshot


def _working_set_patch(
    *,
    context: DirectExecutionContext,
    output: dict[str, Any],
    artifacts: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    set_fields, append_fields, resolutions = parse_executor_working_set_patch(
        output,
        allow_current_judgment=True,
    )
    set_fields.update(
        resolve_working_set_fields(
            current_fields={
                "unknowns": list(context.working_set.unknowns),
                "active_obligations": list(
                    context.working_set.active_obligations
                ),
            },
            resolutions=resolutions,
            resolved_by=context.plan.governance_role,
            source_run_id=context.run_id,
            source_node_id="execute.direct",
        )
    )
    if "current_judgment" in set_fields:
        set_fields["current_judgment"] = {
            **set_fields["current_judgment"],
            "source_run_id": context.run_id,
            "source_node_id": "execute.direct",
            "source_role": context.plan.governance_role,
        }
    finalized_handoffs = finalize_runtime_handoff_claim(
        values=context.working_set.next_candidate_moves,
        run_id=context.run_id,
        outcome="succeeded",
    )
    if finalized_handoffs is not None:
        set_fields["next_candidate_moves"] = finalized_handoffs
    artifact_refs = [
        {
            "run_id": context.run_id,
            "kind": artifact["kind"],
            "path": artifact["path"],
            "sha256": artifact["sha256"],
        }
        for artifact in artifacts
    ]
    append_fields.setdefault("artifact_refs", []).extend(artifact_refs)
    append_fields.setdefault("knowledge_refs", []).extend(context.plan.input_refs)
    append_fields.setdefault("observed_experience", []).append(
        {
            "kind": "direct_run_result",
            "run_id": context.run_id,
            "executor_id": context.plan.executor_id,
            "governance_role": context.plan.governance_role,
            "status": "succeeded",
            "result_status": output.get("status"),
            "artifact_count": len(artifact_refs),
        }
    )
    derived_append = derive_execution_working_set_append(
        run_id=context.run_id,
        node_id="execute.direct",
        node_kind="deterministic_tool",
        governance_role=context.plan.governance_role,
        node_status="succeeded",
        output=output,
    )
    for key, values in derived_append.items():
        append_fields.setdefault(key, []).extend(values)
    return set_fields, append_fields


class DirectRunEngine:
    """Execute exactly one local-read plan and revise its Task Working Set."""

    def __init__(
        self,
        *,
        registry: ExecutorRegistry,
        run_root: str | Path,
        working_sets: WorkingSetStore,
    ) -> None:
        self.registry = registry
        self.run_root = Path(run_root).expanduser().resolve()
        self.working_sets = working_sets

    def run(self, plan: DirectRunPlan, *, task_id: str) -> dict[str, Any]:
        plan.require_valid()
        self.registry.require_compatible_domain_pack(
            plan.metadata.get("domain_pack")
        )
        if plan.side_effect_class not in {"none", "local_read"}:
            raise ValueError(
                "Direct Run only permits none or local_read business side effects"
            )
        task, working_set = self.working_sets.load_context(task_id)
        run_dir = self.run_root / plan.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        summary_path = run_dir / "run_summary.json"
        if summary_path.exists():
            raise FileExistsError(
                "Direct Run has already produced a terminal summary; "
                f"use a fresh run_id: {summary_path}"
            )
        _write_json_if_same(run_dir / "direct_run_plan.json", plan.to_dict())
        _write_json_if_same(run_dir / "context" / "task.json", task.to_dict())
        _write_json_if_same(
            run_dir / "context" / "working_set.json",
            working_set.to_dict(),
        )

        started_at = utc_now()
        context = DirectExecutionContext(
            run_id=plan.run_id,
            plan=plan,
            run_dir=run_dir,
            task=task,
            working_set=working_set,
        )
        try:
            executor = self.registry.resolve(plan.executor_id)
            result = normalize_execution_result(executor(context))
            if result.status != "succeeded":
                raise ValueError(
                    "Direct Run executors must terminate as succeeded or raise; "
                    f"got {result.status}"
                )
            artifacts = validate_run_artifacts(result.artifacts, run_dir)
            artifact_manifest = {
                "version": 1,
                "run_id": plan.run_id,
                "artifacts": artifacts,
            }
            manifest_path = _atomic_write_json(
                run_dir / "artifact_manifest.json",
                artifact_manifest,
            )
            manifest_artifact = validate_run_artifacts(
                ({"kind": "artifact_manifest", "path": str(manifest_path)},),
                run_dir,
            )[0]
            all_artifacts = [*artifacts, manifest_artifact]
            set_fields, append_fields = _working_set_patch(
                context=context,
                output=result.output,
                artifacts=all_artifacts,
            )
            updated_task, updated_working_set = self.working_sets.update_working_set(
                task_id=task_id,
                expected_version=working_set.version,
                revised_by=plan.governance_role,
                revision_reason="direct_run_completed",
                source_run_id=plan.run_id,
                set_fields=set_fields,
                append_fields=append_fields,
            )
            summary = {
                "version": 1,
                "status": "succeeded",
                "run_id": plan.run_id,
                "task_id": task_id,
                "executor_id": plan.executor_id,
                "governance_role": plan.governance_role,
                "agent_instance_id": plan.agent_instance_id,
                "started_at": started_at,
                "finished_at": utc_now(),
                "input_working_set_version": working_set.version,
                "output_working_set_version": updated_working_set.version,
                "task_status": updated_task.status,
                "output": result.output,
                "artifacts": all_artifacts,
                "graph_state_created": False,
                "event_log_created": False,
                "activation_manifest_created": False,
            }
            _atomic_write_json(summary_path, summary)
            return summary
        except Exception as exc:
            error = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            failure_working_set_version: int | None = None
            failure_working_set_error: dict[str, str] | None = None
            try:
                failure_set_fields: dict[str, Any] = {}
                released_handoffs = finalize_runtime_handoff_claim(
                    values=working_set.next_candidate_moves,
                    run_id=plan.run_id,
                    outcome="failed",
                )
                if released_handoffs is not None:
                    failure_set_fields["next_candidate_moves"] = (
                        released_handoffs
                    )
                _, failed_working_set = self.working_sets.update_working_set(
                    task_id=task_id,
                    expected_version=working_set.version,
                    revised_by=plan.governance_role,
                    revision_reason="direct_run_failed",
                    source_run_id=plan.run_id,
                    set_fields=failure_set_fields,
                    append_fields={
                        "observed_experience": [
                            {
                                "kind": "direct_run_result",
                                "run_id": plan.run_id,
                                "executor_id": plan.executor_id,
                                "governance_role": plan.governance_role,
                                "status": "failed",
                                "error": error,
                            }
                        ],
                        "revision_triggers": [
                            {
                                "kind": "direct_run_failure_revision",
                                "run_id": plan.run_id,
                                "executor_id": plan.executor_id,
                                "condition": error,
                            }
                        ],
                    },
                    task_status="active",
                )
                failure_working_set_version = failed_working_set.version
            except Exception as update_error:
                failure_working_set_error = {
                    "type": type(update_error).__name__,
                    "message": str(update_error),
                }
            _atomic_write_json(
                summary_path,
                {
                    "version": 1,
                    "status": "failed",
                    "run_id": plan.run_id,
                    "task_id": task_id,
                    "executor_id": plan.executor_id,
                    "governance_role": plan.governance_role,
                    "started_at": started_at,
                    "finished_at": utc_now(),
                    "input_working_set_version": working_set.version,
                    "error": error,
                    "output_working_set_version": failure_working_set_version,
                    "working_set_update_error": failure_working_set_error,
                    "graph_state_created": False,
                    "event_log_created": False,
                    "activation_manifest_created": False,
                },
            )
            raise


def build_direct_executor_registry(
    *,
    domain_pack: "DomainPack",
) -> ExecutorRegistry:
    from planner.executor_bindings import build_executor_registry

    return build_executor_registry(
        domain_pack,
        runtime_kind="direct_run",
    )
