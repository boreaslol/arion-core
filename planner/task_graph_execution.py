"""Coordinate TaskGraph execution with versioned Working Set continuity."""

from __future__ import annotations

from collections import Counter
import json
from typing import Any

from planner.unified_execution import UnifiedExecutionEngine, sha256_file
from planner.working_set_semantics import (
    derive_execution_working_set_append,
    finalize_runtime_handoff_claim,
    parse_executor_working_set_patch,
    reconcile_runtime_handoff_claim,
    resolve_working_set_fields,
)
from planner.working_set_store import (
    WorkingSetConflictError,
    WorkingSetSnapshot,
    WorkingSetStore,
)

_RESOLVED_ITEM_STATUSES = frozenset(
    {
        "closed",
        "falsified",
        "rejected",
        "resolved",
        "superseded",
        "tested",
    }
)
_CLOSURE_DISPOSITIONS = frozenset(
    {
        "none_open",
        "accepted_with_obligations",
    }
)


def _checkpoint_id(snapshot: dict[str, Any]) -> str:
    return (
        f"task_graph:{snapshot['run_id']}:"
        f"events:{snapshot['event_count']}:status:{snapshot['status']}"
    )


def _checkpoint_exists(
    working_set: WorkingSetSnapshot,
    checkpoint_id: str,
) -> bool:
    return any(
        isinstance(item, dict) and item.get("checkpoint_id") == checkpoint_id
        for item in working_set.observed_experience
    )


def _task_status(
    snapshot: dict[str, Any],
    *,
    closure_node_id: str | None,
    closure_contract: dict[str, Any] | None,
) -> str:
    runtime_status = str(snapshot["status"])
    if runtime_status == "waiting":
        return "waiting"
    if runtime_status == "succeeded":
        closure = next(
            (
                node
                for node in snapshot["nodes"]
                if node["node_id"] == closure_node_id
            ),
            None,
        )
        if (
            closure
            and closure["status"] == "succeeded"
            and closure_contract is not None
        ):
            return "closed"
    return "active"


def _is_open_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return True
    return (
        str(item.get("status") or "open").strip().casefold()
        not in _RESOLVED_ITEM_STATUSES
    )


def closure_open_item_counts(
    working_set: WorkingSetSnapshot,
) -> dict[str, int]:
    return {
        "unknowns": sum(
            1 for item in working_set.unknowns if _is_open_item(item)
        ),
        "counterfactuals": sum(
            1 for item in working_set.counterfactuals if _is_open_item(item)
        ),
    }


def _closure_entries(value: Any, *, field_name: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"closure {field_name} must be a list")
    normalized: list[Any] = []
    for item in value:
        if isinstance(item, dict):
            if not item:
                raise ValueError(
                    f"closure {field_name} entries must not be empty"
                )
            normalized.append(dict(item))
            continue
        text = str(item).strip()
        if not text:
            raise ValueError(
                f"closure {field_name} entries must not be empty"
            )
        normalized.append(text)
    return normalized


def validate_closure_output(
    *,
    working_set: WorkingSetSnapshot,
    output: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = dict(output or {})
    if str(payload.get("decision") or "").strip().upper() != "CLOSED":
        raise ValueError("task closure requires decision=CLOSED")
    judgment = str(payload.get("judgment") or "").strip()
    if not judgment:
        raise ValueError("task closure requires an explicit judgment")
    raw_disposition = payload.get("open_item_disposition")
    if not isinstance(raw_disposition, dict):
        raise ValueError("task closure requires open_item_disposition")
    if set(raw_disposition) != {"unknowns", "counterfactuals", "reason"}:
        raise ValueError(
            "open_item_disposition must contain exactly unknowns, "
            "counterfactuals, and reason"
        )
    reason = str(raw_disposition.get("reason") or "").strip()
    if not reason:
        raise ValueError("open_item_disposition.reason is required")

    open_counts = closure_open_item_counts(working_set)
    disposition: dict[str, str] = {"reason": reason}
    for field_name in ("unknowns", "counterfactuals"):
        value = str(raw_disposition.get(field_name) or "").strip()
        if value not in _CLOSURE_DISPOSITIONS:
            raise ValueError(
                f"unsupported closure disposition for {field_name}: {value}"
            )
        expected = (
            "accepted_with_obligations"
            if open_counts[field_name]
            else "none_open"
        )
        if value != expected:
            raise ValueError(
                f"closure disposition for {field_name} must be {expected}; "
                f"open_count={open_counts[field_name]}"
            )
        disposition[field_name] = value

    revision_triggers = _closure_entries(
        payload.get("revision_triggers"),
        field_name="revision_triggers",
    )
    if not revision_triggers:
        raise ValueError(
            "task closure requires at least one revision trigger"
        )
    observation_obligations = _closure_entries(
        payload.get("observation_obligations"),
        field_name="observation_obligations",
    )
    if any(open_counts.values()) and not observation_obligations:
        raise ValueError(
            "open unknowns or counterfactuals require an observation "
            "obligation before closure"
        )
    return {
        "decision": "CLOSED",
        "judgment": judgment,
        "open_item_disposition": disposition,
        "open_item_counts": open_counts,
        "revision_triggers": revision_triggers,
        "observation_obligations": observation_obligations,
    }


def _active_obligations(
    working_set: WorkingSetSnapshot,
    snapshot: dict[str, Any],
    *,
    closure_contract: dict[str, Any] | None,
    closure_node_id: str | None,
) -> list[Any]:
    run_id = str(snapshot["run_id"])
    preserved = [
        item
        for item in working_set.active_obligations
        if not (
            isinstance(item, dict)
            and item.get("kind") == "task_graph_wait"
            and item.get("run_id") == run_id
        )
    ]
    for node in snapshot["nodes"]:
        if node["status"] not in {"waiting_human", "waiting_external"}:
            continue
        output = dict(node.get("output") or {})
        preserved.append(
            {
                "kind": "task_graph_wait",
                "run_id": run_id,
                "node_id": node["node_id"],
                "status": node["status"],
                "reason": output.get("reason"),
                "gate": output.get("gate"),
                "assignee_role": output.get("assignee_role"),
            }
        )
    if closure_contract is not None:
        for obligation in closure_contract["observation_obligations"]:
            preserved.append(
                {
                    "kind": "judgment_observation_obligation",
                    "run_id": run_id,
                    "node_id": closure_node_id,
                    "status": "open",
                    "obligation": obligation,
                }
            )
    return preserved


def _artifact_refs(
    engine: UnifiedExecutionEngine,
    run_id: str,
) -> list[dict[str, Any]]:
    refs = [
        {
            "run_id": run_id,
            "node_id": artifact.get("node_id"),
            "kind": artifact["kind"],
            "path": artifact["path"],
            "sha256": artifact.get("sha256"),
        }
        for artifact in engine.store.artifacts(run_id)
    ]
    run_dir = engine.run_root / run_id
    for kind, path in (
        ("runtime_snapshot", run_dir / "runtime_snapshot.json"),
        ("runtime_events", run_dir / "runtime_events.jsonl"),
    ):
        if path.is_file():
            refs.append(
                {
                    "run_id": run_id,
                    "node_id": None,
                    "kind": kind,
                    "path": str(path.resolve()),
                    "sha256": sha256_file(path),
                }
            )
    return refs


def _replace_run_artifact_refs(
    working_set: WorkingSetSnapshot,
    *,
    run_id: str,
    current_refs: list[dict[str, Any]],
) -> list[Any]:
    preserved = [
        item
        for item in working_set.artifact_refs
        if not (
            isinstance(item, dict)
            and str(item.get("run_id") or "") == run_id
        )
    ]
    return [*preserved, *current_refs]


def _deduplicate_append_fields(
    working_set: WorkingSetSnapshot,
    append_fields: dict[str, list[Any]],
) -> dict[str, list[Any]]:
    deduplicated: dict[str, list[Any]] = {}
    for field_name, values in append_fields.items():
        existing = getattr(working_set, field_name, ())
        seen = {
            json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            for item in existing
        }
        retained: list[Any] = []
        for item in values:
            identity = json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if identity in seen:
                continue
            seen.add(identity)
            retained.append(item)
        if retained:
            deduplicated[field_name] = retained
    return deduplicated


def _revision_triggers(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    triggers: list[dict[str, Any]] = []
    for node in snapshot["nodes"]:
        if node["status"] in {"waiting_human", "waiting_external"}:
            triggers.append(
                {
                    "kind": "task_graph_wait_resolution",
                    "run_id": snapshot["run_id"],
                    "node_id": node["node_id"],
                    "condition": node["status"],
                }
            )
        elif node["status"] == "failed":
            triggers.append(
                {
                    "kind": "task_graph_failure_revision",
                    "run_id": snapshot["run_id"],
                    "node_id": node["node_id"],
                    "condition": dict(node.get("error") or {}),
                }
            )
    return triggers


def _active_revision_triggers(
    working_set: WorkingSetSnapshot,
    snapshot: dict[str, Any],
    *,
    closure_contract: dict[str, Any] | None,
    closure_node_id: str | None,
) -> list[Any]:
    run_id = str(snapshot["run_id"])
    preserved = [
        item
        for item in working_set.revision_triggers
        if not (isinstance(item, dict) and item.get("run_id") == run_id)
    ]
    current = [*preserved, *_revision_triggers(snapshot)]
    if closure_contract is not None:
        current.extend(
            {
                "kind": "judgment_revision_condition",
                "run_id": run_id,
                "node_id": closure_node_id,
                "condition": trigger,
            }
            for trigger in closure_contract["revision_triggers"]
        )
    return current


def _semantic_working_set_changes(
    engine: UnifiedExecutionEngine,
    snapshot: dict[str, Any],
    *,
    working_set: WorkingSetSnapshot,
    active_obligations: list[Any],
) -> tuple[dict[str, list[Any]], dict[str, list[Any]]]:
    graph = engine.store.get_graph(str(snapshot["run_id"]))
    definitions = {node.node_id: node for node in graph.nodes}
    set_fields: dict[str, list[Any]] = {}
    append_fields: dict[str, list[Any]] = {}
    current_fields = {
        "unknowns": list(working_set.unknowns),
        "active_obligations": list(active_obligations),
    }
    for node in snapshot["nodes"]:
        definition = definitions.get(str(node["node_id"]))
        if definition is None:
            continue
        output = dict(node.get("output") or {})
        if node["status"] == "failed":
            output.setdefault("status", "failed")
            output.setdefault("error", dict(node.get("error") or {}))
        _, requested_append, resolutions = parse_executor_working_set_patch(
            output,
            allow_current_judgment=False,
        )
        resolved_fields = resolve_working_set_fields(
            current_fields=current_fields,
            resolutions=resolutions,
            resolved_by=definition.assignee_role,
            source_run_id=str(snapshot["run_id"]),
            source_node_id=definition.node_id,
        )
        current_fields.update(resolved_fields)
        set_fields.update(resolved_fields)
        for key, values in requested_append.items():
            append_fields.setdefault(key, []).extend(values)
        patch = derive_execution_working_set_append(
            run_id=str(snapshot["run_id"]),
            node_id=definition.node_id,
            node_kind=definition.kind,
            governance_role=definition.assignee_role,
            node_status=str(node["status"]),
            output=output,
        )
        for key, values in patch.items():
            append_fields.setdefault(key, []).extend(values)
    return set_fields, append_fields


def _current_judgment(
    working_set: WorkingSetSnapshot,
    snapshot: dict[str, Any],
    *,
    closure_node_id: str | None,
    closure_contract: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if closure_contract is None:
        return None
    return {
        **working_set.current_judgment,
        "status": "closed",
        "statement": closure_contract["judgment"],
        "open_item_disposition": closure_contract[
            "open_item_disposition"
        ],
        "open_item_counts": closure_contract["open_item_counts"],
        "revision_triggers": closure_contract["revision_triggers"],
        "observation_obligations": closure_contract[
            "observation_obligations"
        ],
        "source_run_id": snapshot["run_id"],
        "source_node_id": closure_node_id,
    }


class TaskGraphExecutionCoordinator:
    """Run or resume a graph and materialize its state into the Working Set."""

    def __init__(
        self,
        *,
        engine: UnifiedExecutionEngine,
        working_sets: WorkingSetStore,
    ) -> None:
        self.engine = engine
        self.working_sets = working_sets

    def run(
        self,
        graph,
        *,
        task_id: str,
        max_nodes: int | None = None,
    ) -> dict[str, Any]:
        snapshot = self.engine.run(graph, max_nodes=max_nodes)
        return self.synchronize(task_id=task_id, snapshot=snapshot)

    def resume(
        self,
        run_id: str,
        *,
        task_id: str,
        max_nodes: int | None = None,
    ) -> dict[str, Any]:
        snapshot = self.engine.resume(run_id, max_nodes=max_nodes)
        return self.synchronize(task_id=task_id, snapshot=snapshot)

    def resolve(
        self,
        run_id: str,
        node_id: str,
        *,
        task_id: str,
        actor: str,
        decision: str,
        output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        graph = self.engine.store.get_graph(run_id)
        self.engine.registry.require_compatible_domain_pack(
            graph.metadata.get("domain_pack")
        )
        definition = next(
            (node for node in graph.nodes if node.node_id == node_id),
            None,
        )
        if definition is None:
            raise ValueError(f"unknown TaskGraph node: {node_id}")
        if (
            decision == "approve"
            and definition.kind == "human_gate"
            and definition.metadata.get("closes_task") is True
        ):
            _, working_set = self.working_sets.load_context(task_id)
            validate_closure_output(
                working_set=working_set,
                output=output,
            )
        self.engine.store.resolve_waiting_node(
            run_id,
            node_id,
            actor=actor,
            decision=decision,
            output=output,
        )
        return self.synchronize(
            task_id=task_id,
            snapshot=self.engine.store.run_snapshot(run_id),
        )

    def synchronize(
        self,
        *,
        task_id: str,
        snapshot: dict[str, Any],
        handoff_reconciliation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_id = str(snapshot["run_id"])
        graph = self.engine.store.get_graph(run_id)
        closure_node_ids = [
            node.node_id
            for node in graph.nodes
            if node.kind == "human_gate"
            and node.metadata.get("closes_task") is True
        ]
        if len(closure_node_ids) > 1:
            raise ValueError("TaskGraph defines multiple task-closure gates")
        closure_node_id = (
            closure_node_ids[0] if closure_node_ids else None
        )
        self.engine.registry.require_compatible_domain_pack(
            graph.metadata.get("domain_pack")
        )
        if (
            handoff_reconciliation is not None
            and str(snapshot["status"]) != "succeeded"
        ):
            raise ValueError(
                "handoff success reconciliation requires a succeeded "
                f"TaskGraph; got {snapshot['status']}"
            )
        self.engine.store.export_canonical_views(
            run_id,
            self.engine.run_root / run_id,
        )
        checkpoint_id = _checkpoint_id(snapshot)
        for _ in range(3):
            _, working_set = self.working_sets.load_context(task_id)
            closure_contract = None
            if closure_node_id is not None:
                closure = next(
                    (
                        node
                        for node in snapshot["nodes"]
                        if node["node_id"] == closure_node_id
                        and node["status"] == "succeeded"
                    ),
                    None,
                )
                if closure is not None:
                    closure_contract = validate_closure_output(
                        working_set=working_set,
                        output=dict(closure.get("output") or {}),
                    )
            if (
                handoff_reconciliation is None
                and _checkpoint_exists(working_set, checkpoint_id)
            ):
                return {
                    **snapshot,
                    "task_id": task_id,
                    "working_set": {
                        "updated": False,
                        "version": working_set.version,
                        "checkpoint_id": checkpoint_id,
                    },
                }

            status_counts = Counter(
                str(node["status"]) for node in snapshot["nodes"]
            )
            active_obligations = _active_obligations(
                working_set,
                snapshot,
                closure_contract=closure_contract,
                closure_node_id=closure_node_id,
            )
            set_fields: dict[str, Any] = {
                "active_obligations": active_obligations,
                "artifact_refs": _replace_run_artifact_refs(
                    working_set,
                    run_id=run_id,
                    current_refs=_artifact_refs(self.engine, run_id),
                ),
                "revision_triggers": _active_revision_triggers(
                    working_set,
                    snapshot,
                    closure_contract=closure_contract,
                    closure_node_id=closure_node_id,
                ),
            }
            judgment = _current_judgment(
                working_set,
                snapshot,
                closure_node_id=closure_node_id,
                closure_contract=closure_contract,
            )
            if judgment is not None:
                set_fields["current_judgment"] = judgment
            if handoff_reconciliation is not None:
                finalized_handoffs = reconcile_runtime_handoff_claim(
                    values=working_set.next_candidate_moves,
                    run_id=run_id,
                    reconciled_by=str(
                        handoff_reconciliation.get("actor") or ""
                    ),
                    reconciliation_reason=str(
                        handoff_reconciliation.get("reason") or ""
                    ),
                    reconciliation_evidence=dict(
                        handoff_reconciliation.get("evidence") or {}
                    ),
                )
            else:
                finalized_handoffs = finalize_runtime_handoff_claim(
                    values=working_set.next_candidate_moves,
                    run_id=run_id,
                    outcome=str(snapshot["status"]),
                )
            if finalized_handoffs is not None:
                set_fields["next_candidate_moves"] = finalized_handoffs
            append_fields = {
                "observed_experience": [
                    {
                        "kind": "task_graph_checkpoint",
                        "checkpoint_id": checkpoint_id,
                        "run_id": run_id,
                        "status": snapshot["status"],
                        "event_count": snapshot["event_count"],
                        "artifact_count": snapshot["artifact_count"],
                        "node_status_counts": dict(sorted(status_counts.items())),
                    }
                ],
            }
            semantic_set, semantic_append = _semantic_working_set_changes(
                self.engine,
                snapshot,
                working_set=working_set,
                active_obligations=active_obligations,
            )
            set_fields.update(semantic_set)
            for key, values in semantic_append.items():
                append_fields.setdefault(key, []).extend(values)
            append_fields = _deduplicate_append_fields(
                working_set,
                append_fields,
            )
            try:
                revised_by = (
                    str(handoff_reconciliation.get("actor") or "")
                    if handoff_reconciliation is not None
                    else "Arion Runtime"
                )
                revision_reason = (
                    "runtime_handoff_success_reconciled"
                    if handoff_reconciliation is not None
                    else "task_graph_checkpoint"
                )
                updated_task, updated_working_set = (
                    self.working_sets.update_working_set(
                        task_id=task_id,
                        expected_version=working_set.version,
                        revised_by=revised_by,
                        revision_reason=revision_reason,
                        source_run_id=run_id,
                        set_fields=set_fields,
                        append_fields=append_fields,
                        task_status=_task_status(
                            snapshot,
                            closure_node_id=closure_node_id,
                            closure_contract=closure_contract,
                        ),
                    )
                )
            except WorkingSetConflictError:
                continue
            return {
                **snapshot,
                "task_id": task_id,
                "working_set": {
                    "updated": True,
                    "version": updated_working_set.version,
                    "checkpoint_id": checkpoint_id,
                    "task_status": updated_task.status,
                },
            }
        raise WorkingSetConflictError(
            f"unable to synchronize TaskGraph {run_id} after concurrent revisions"
        )
