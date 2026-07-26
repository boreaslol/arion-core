"""Translate plans and runtime outputs into bounded Working Set semantics."""

from __future__ import annotations

import json
from typing import Any

from planner.runtime_outcome import normalize_runtime_outcome


EXECUTOR_SEMANTIC_APPEND_FIELDS = frozenset(
    {
        "observed_experience",
        "knowns",
        "unknowns",
        "assumptions",
        "competing_hypotheses",
        "counterfactuals",
        "asymmetric_consequences",
        "revision_triggers",
        "next_candidate_moves",
    }
)
EXECUTOR_RESOLVABLE_FIELDS = frozenset(
    {
        "unknowns",
        "active_obligations",
    }
)
RESOLUTION_SELECTOR_FIELDS = frozenset(
    {
        "item_id",
        "kind",
        "source_role",
        "statement",
        "run_id",
        "node_id",
        "condition",
        "assignee_role",
    }
)
RESOLUTION_IDENTITY_FIELDS = frozenset(
    {
        "item_id",
        "statement",
        "run_id",
        "node_id",
        "condition",
    }
)


def parse_executor_working_set_patch(
    output: dict[str, Any],
    *,
    allow_current_judgment: bool,
) -> tuple[
    dict[str, Any],
    dict[str, list[Any]],
    dict[str, list[dict[str, Any]]],
]:
    """Validate the semantic state an executor may propose."""

    if "working_set_patch" not in output or output["working_set_patch"] is None:
        return {}, {}, {}
    requested = output["working_set_patch"]
    if not isinstance(requested, dict):
        raise ValueError("working_set_patch must be a mapping")
    unknown_sections = set(requested) - {"set", "append", "resolve"}
    if unknown_sections:
        raise ValueError(
            "unsupported working_set_patch sections: "
            + ", ".join(sorted(unknown_sections))
        )

    raw_set = requested.get("set", {})
    raw_append = requested.get("append", {})
    raw_resolve = requested.get("resolve", {})
    if raw_set is None:
        raw_set = {}
    if raw_append is None:
        raw_append = {}
    if raw_resolve is None:
        raw_resolve = {}
    if not isinstance(raw_set, dict):
        raise ValueError("working_set_patch.set must be a mapping")
    if not isinstance(raw_append, dict):
        raise ValueError("working_set_patch.append must be a mapping")
    if not isinstance(raw_resolve, dict):
        raise ValueError("working_set_patch.resolve must be a mapping")

    unknown_set = set(raw_set) - {"current_judgment"}
    if unknown_set:
        raise ValueError(
            "unsupported working_set_patch.set fields: "
            + ", ".join(sorted(unknown_set))
        )
    if raw_set and not allow_current_judgment:
        raise ValueError(
            "TaskGraph executors cannot set current_judgment; "
            "orchestrator closure owns collective judgment"
        )
    set_fields: dict[str, Any] = {}
    if "current_judgment" in raw_set:
        judgment = raw_set["current_judgment"]
        if not isinstance(judgment, dict):
            raise ValueError(
                "working_set_patch.set.current_judgment must be a mapping"
            )
        set_fields["current_judgment"] = dict(judgment)

    unknown_append = set(raw_append) - EXECUTOR_SEMANTIC_APPEND_FIELDS
    if unknown_append:
        raise ValueError(
            "unsupported working_set_patch.append fields: "
            + ", ".join(sorted(unknown_append))
        )
    append_fields: dict[str, list[Any]] = {}
    for field_name, values in raw_append.items():
        if not isinstance(values, (list, tuple)):
            raise ValueError(
                f"working_set_patch.append.{field_name} must be a list"
            )
        append_fields[field_name] = list(values)

    unknown_resolve = set(raw_resolve) - EXECUTOR_RESOLVABLE_FIELDS
    if unknown_resolve:
        raise ValueError(
            "unsupported working_set_patch.resolve fields: "
            + ", ".join(sorted(unknown_resolve))
        )
    resolve_fields: dict[str, list[dict[str, Any]]] = {}
    for field_name, directives in raw_resolve.items():
        if not isinstance(directives, (list, tuple)):
            raise ValueError(
                f"working_set_patch.resolve.{field_name} must be a list"
            )
        normalized: list[dict[str, Any]] = []
        for directive in directives:
            if not isinstance(directive, dict):
                raise ValueError(
                    f"working_set_patch.resolve.{field_name} entries "
                    "must be mappings"
                )
            unknown_directive = set(directive) - {"selector", "resolution"}
            if unknown_directive:
                raise ValueError(
                    f"unsupported working_set_patch.resolve.{field_name} "
                    "entry fields: "
                    + ", ".join(sorted(unknown_directive))
                )
            selector = directive.get("selector")
            if not isinstance(selector, dict) or not selector:
                raise ValueError(
                    f"working_set_patch.resolve.{field_name}.selector "
                    "must be a non-empty mapping"
                )
            unknown_selector = set(selector) - RESOLUTION_SELECTOR_FIELDS
            if unknown_selector:
                raise ValueError(
                    f"unsupported working_set_patch.resolve.{field_name} "
                    "selector fields: "
                    + ", ".join(sorted(unknown_selector))
                )
            if not set(selector) & RESOLUTION_IDENTITY_FIELDS:
                raise ValueError(
                    f"working_set_patch.resolve.{field_name}.selector "
                    "must include an exact identity field"
                )
            if any(
                value is None or isinstance(value, (dict, list, tuple))
                for value in selector.values()
            ):
                raise ValueError(
                    f"working_set_patch.resolve.{field_name}.selector "
                    "values must be non-null scalars"
                )
            resolution = str(directive.get("resolution") or "").strip()
            if not resolution:
                raise ValueError(
                    f"working_set_patch.resolve.{field_name}.resolution "
                    "is required"
                )
            normalized.append(
                {
                    "selector": dict(selector),
                    "resolution": resolution,
                }
            )
        resolve_fields[field_name] = normalized
    return set_fields, append_fields, resolve_fields


def _strings(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if value:
        return [str(value).strip()]
    return []


def _semantic_values(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    if value is None:
        return []
    return [value]


def _deduplicate_semantics(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    deduplicated: list[Any] = []
    for value in values:
        identity = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if identity in seen:
            continue
        seen.add(identity)
        deduplicated.append(value)
    return deduplicated


def _counterfactual(role: str, hypothesis: str) -> dict[str, Any]:
    return {
        "kind": "hypothesis_falsification_requirement",
        "source_role": role,
        "hypothesis": hypothesis,
        "required_observation": (
            "Record evidence that would falsify or materially weaken this "
            "hypothesis before closure."
        ),
        "status": "untested",
    }


def build_role_runtime_working_set_patch(
    *,
    output: dict[str, Any],
    governance_role: str,
    run_id: str | None = None,
    node_id: str | None = None,
) -> dict[str, Any]:
    """Compile one governed role result into the explicit semantic patch."""

    _, append_fields, resolve_fields = parse_executor_working_set_patch(
        output,
        allow_current_judgment=False,
    )
    provenance: dict[str, Any] = {"source_role": governance_role}
    if run_id:
        provenance["run_id"] = run_id
    if node_id:
        provenance["node_id"] = node_id

    for observation in _semantic_values(output.get("observed_experience")):
        if observation in (None, ""):
            continue
        record: dict[str, Any] = {
            "kind": "runtime_observed_experience",
            **provenance,
        }
        if isinstance(observation, dict):
            record["observation"] = dict(observation)
        else:
            record["statement"] = str(observation).strip()
        append_fields.setdefault("observed_experience", []).append(record)

    for trigger in _semantic_values(output.get("revision_triggers")):
        if trigger in (None, ""):
            continue
        record = {
            "kind": "runtime_revision_trigger",
            **provenance,
        }
        if isinstance(trigger, dict):
            record["trigger"] = dict(trigger)
        else:
            record["condition"] = str(trigger).strip()
        append_fields.setdefault("revision_triggers", []).append(record)

    for consequence in _semantic_values(
        output.get("asymmetric_consequences")
    ):
        if consequence in (None, ""):
            continue
        record = {
            "kind": "runtime_asymmetric_consequence",
            **provenance,
        }
        if isinstance(consequence, dict):
            record["consequence"] = dict(consequence)
        else:
            record["statement"] = str(consequence).strip()
        append_fields.setdefault("asymmetric_consequences", []).append(record)

    for statement in _strings(output.get("known_facts")):
        append_fields.setdefault("knowns", []).append(
            {
                "kind": "runtime_known_fact",
                **provenance,
                "statement": statement,
            }
        )
    for statement in _strings(output.get("resolved_scope")):
        append_fields.setdefault("knowns", []).append(
            {
                "kind": "runtime_resolved_scope",
                **provenance,
                "statement": statement,
            }
        )

    for hypothesis in _strings(output.get("hypotheses")):
        append_fields.setdefault("competing_hypotheses", []).append(
            {
                "kind": "runtime_candidate_hypothesis",
                **provenance,
                "statement": hypothesis,
                "status": "untested",
            }
        )
        counterfactual = _counterfactual(governance_role, hypothesis)
        if run_id:
            counterfactual["run_id"] = run_id
        if node_id:
            counterfactual["node_id"] = node_id
        append_fields.setdefault("counterfactuals", []).append(counterfactual)

    for field_name, kind in (
        ("questions_to_answer", "open_question"),
        ("unresolved_residual", "unresolved_residual"),
        ("unverified_items", "unverified_item"),
        ("unresolved_items", "unresolved_item"),
    ):
        for statement in _strings(output.get(field_name)):
            append_fields.setdefault("unknowns", []).append(
                {
                    "kind": kind,
                    **provenance,
                    "statement": statement,
                }
            )

    handoff_batch_id = ":".join(
        value for value in (run_id, node_id) if value
    )
    handoff_reason = str(output.get("handoff_reason") or "").strip()
    required_owners = list(
        dict.fromkeys(_strings(output.get("required_next_owner")))
    )
    recommended_owners = list(
        dict.fromkeys(
            [
                *_strings(output.get("suggested_next_owner")),
                *_strings(output.get("suggested_next_owners")),
            ]
        )
    )
    recommended_owners = [
        owner for owner in recommended_owners if owner not in required_owners
    ]
    for strength, owners in (
        ("required", required_owners),
        ("recommended", recommended_owners),
    ):
        for rank, next_owner in enumerate(owners, start=1):
            move = {
                "kind": "runtime_handoff_candidate",
                **provenance,
                "from_role": governance_role,
                "to_role": next_owner,
                "handoff_strength": strength,
                "handoff_rank": rank,
                "status": "open",
            }
            if handoff_batch_id:
                move["handoff_batch_id"] = handoff_batch_id
            if handoff_reason:
                move["reason"] = handoff_reason
            append_fields.setdefault("next_candidate_moves", []).append(move)

    patch: dict[str, Any] = {"append": append_fields}
    if resolve_fields:
        patch["resolve"] = resolve_fields
    return patch


def resolve_working_set_fields(
    *,
    current_fields: dict[str, list[Any]],
    resolutions: dict[str, list[dict[str, Any]]],
    resolved_by: str,
    source_run_id: str,
    source_node_id: str,
) -> dict[str, list[Any]]:
    """Resolve exactly one matching open item per explicit directive."""

    updates: dict[str, list[Any]] = {}
    for field_name, directives in resolutions.items():
        items = list(updates.get(field_name, current_fields.get(field_name, [])))
        for directive in directives:
            selector = dict(directive["selector"])
            matches = [
                index
                for index, item in enumerate(items)
                if isinstance(item, dict)
                and all(item.get(key) == value for key, value in selector.items())
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"working_set_patch.resolve.{field_name} selector "
                    f"matched {len(matches)} items; expected exactly 1: "
                    f"{selector}"
                )
            index = matches[0]
            current = dict(items[index])
            resolution = str(directive["resolution"])
            if str(current.get("status") or "").casefold() == "resolved":
                if current.get("resolution") != resolution:
                    raise ValueError(
                        f"working_set_patch.resolve.{field_name} conflicts "
                        f"with the existing resolution: {selector}"
                    )
                continue
            items[index] = {
                **current,
                "status": "resolved",
                "resolution": resolution,
                "resolved_by": resolved_by,
                "source_run_id": source_run_id,
                "source_node_id": source_node_id,
            }
        updates[field_name] = items
    return updates


def latest_runtime_handoff_batch(values: list[Any] | tuple[Any, ...]) -> list[dict[str, Any]]:
    runtime_moves = [
        dict(item)
        for item in values
        if isinstance(item, dict)
        and item.get("kind") == "runtime_handoff_candidate"
    ]
    if not runtime_moves:
        return []
    latest = runtime_moves[-1]
    batch_id = str(latest.get("handoff_batch_id") or "")
    if not batch_id:
        return [latest]
    return [
        item
        for item in runtime_moves
        if str(item.get("handoff_batch_id") or "") == batch_id
    ]


def active_runtime_handoff_claim(
    values: list[Any] | tuple[Any, ...],
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in latest_runtime_handoff_batch(values)
            if str(item.get("status") or "").casefold() == "claimed"
        ),
        None,
    )


def claim_runtime_handoff(
    *,
    values: list[Any] | tuple[Any, ...],
    selector: dict[str, Any],
    run_id: str,
    claimed_role: str,
) -> list[Any]:
    items = list(values)
    matches = [
        index
        for index, item in enumerate(items)
        if isinstance(item, dict)
        and all(item.get(key) == value for key, value in selector.items())
    ]
    if len(matches) != 1:
        raise ValueError(
            f"runtime handoff selector matched {len(matches)} items; "
            f"expected exactly 1: {selector}"
        )
    index = matches[0]
    current = dict(items[index])
    status = str(current.get("status") or "open").casefold()
    if status == "claimed":
        claimed_by_run_id = str(current.get("claimed_by_run_id") or "")
        if claimed_by_run_id == run_id:
            return items
        raise ValueError(
            "runtime handoff is already claimed by "
            f"{claimed_by_run_id or 'another run'}"
        )
    if status != "open":
        raise ValueError(
            f"runtime handoff is not open for claim: status={status}"
        )
    items[index] = {
        **current,
        "status": "claimed",
        "claimed_by_run_id": run_id,
        "claimed_role": claimed_role,
    }
    return items


def finalize_runtime_handoff_claim(
    *,
    values: list[Any] | tuple[Any, ...],
    run_id: str,
    outcome: str,
) -> list[Any] | None:
    if outcome not in {"succeeded", "failed", "preparation_failed"}:
        return None
    items = list(values)
    matches = [
        index
        for index, item in enumerate(items)
        if isinstance(item, dict)
        and str(item.get("status") or "").casefold() == "claimed"
        and item.get("claimed_by_run_id") == run_id
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(
            f"run {run_id} owns {len(matches)} handoff claims; expected 1"
        )
    index = matches[0]
    current = dict(items[index])
    current.pop("claimed_by_run_id", None)
    current.pop("claimed_role", None)
    if outcome == "succeeded":
        current["status"] = "consumed"
        current["consumed_by_run_id"] = run_id
    else:
        current["status"] = "open"
        current["last_claimed_run_id"] = run_id
        current["last_claim_release_reason"] = outcome
    items[index] = current
    return items


def recover_runtime_handoff_claim(
    *,
    values: list[Any] | tuple[Any, ...],
    run_id: str,
    recovered_by: str,
    recovery_reason: str,
    recovery_evidence: dict[str, Any],
) -> list[Any]:
    actor = recovered_by.strip()
    reason = recovery_reason.strip()
    if not actor:
        raise ValueError("recovered_by is required")
    if not reason:
        raise ValueError("recovery_reason is required")
    if not recovery_evidence:
        raise ValueError("recovery_evidence is required")

    items = list(values)
    matches = [
        index
        for index, item in enumerate(items)
        if isinstance(item, dict)
        and str(item.get("status") or "").casefold() == "claimed"
        and item.get("claimed_by_run_id") == run_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"run {run_id} owns {len(matches)} handoff claims; expected 1"
        )

    index = matches[0]
    current = dict(items[index])
    current.pop("claimed_by_run_id", None)
    current.pop("claimed_role", None)
    current.update(
        {
            "status": "open",
            "last_claimed_run_id": run_id,
            "last_claim_release_reason": "stale_claim_recovery",
            "recovered_by": actor,
            "recovery_reason": reason,
            "recovery_evidence": dict(recovery_evidence),
        }
    )
    items[index] = current
    return items


def reconcile_runtime_handoff_claim(
    *,
    values: list[Any] | tuple[Any, ...],
    run_id: str,
    reconciled_by: str,
    reconciliation_reason: str,
    reconciliation_evidence: dict[str, Any],
) -> list[Any]:
    actor = reconciled_by.strip()
    reason = reconciliation_reason.strip()
    if not actor:
        raise ValueError("reconciled_by is required")
    if not reason:
        raise ValueError("reconciliation_reason is required")
    if not reconciliation_evidence:
        raise ValueError("reconciliation_evidence is required")

    items = list(values)
    matches = [
        index
        for index, item in enumerate(items)
        if isinstance(item, dict)
        and str(item.get("status") or "").casefold() == "claimed"
        and item.get("claimed_by_run_id") == run_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"run {run_id} owns {len(matches)} handoff claims; expected 1"
        )

    index = matches[0]
    current = dict(items[index])
    current.pop("claimed_by_run_id", None)
    current.pop("claimed_role", None)
    current.update(
        {
            "status": "consumed",
            "consumed_by_run_id": run_id,
            "success_reconciled_by": actor,
            "success_reconciliation_reason": reason,
            "success_reconciliation_evidence": dict(
                reconciliation_evidence
            ),
        }
    )
    items[index] = current
    return items


def build_initial_working_set(
    *,
    planner_decision: dict[str, Any],
    capability_plan: dict[str, Any],
) -> dict[str, Any]:
    """Seed collective judgment from explicit planning contracts."""

    collaboration = capability_plan.get("collaboration_plan") or {}
    orchestrator_role = str(collaboration.get("lead_role") or "").strip()
    if not orchestrator_role:
        raise ValueError(
            "capability plan collaboration_plan.lead_role is required"
        )
    knowns: list[dict[str, Any]] = []
    unknowns: list[dict[str, Any]] = [
        {
            "kind": "planner_unresolved_item",
            "source_role": orchestrator_role,
            "statement": item,
        }
        for item in _strings(planner_decision.get("unresolved_items"))
    ]
    hypotheses: list[dict[str, Any]] = []
    counterfactuals: list[dict[str, Any]] = []
    asymmetric_consequences: list[dict[str, Any]] = []
    next_candidate_moves: list[dict[str, Any]] = []

    for task_package in collaboration.get("task_packages") or []:
        if not isinstance(task_package, dict):
            continue
        role = str(task_package.get("assignee_role") or "Unassigned")
        for statement in _strings(task_package.get("known_facts")):
            knowns.append(
                {
                    "kind": "planned_known_fact",
                    "source_role": role,
                    "statement": statement,
                }
            )
        for statement in _strings(task_package.get("questions_to_answer")):
            unknowns.append(
                {
                    "kind": "open_question",
                    "source_role": role,
                    "statement": statement,
                }
            )
        for statement in _strings(task_package.get("unresolved_residual")):
            unknowns.append(
                {
                    "kind": "unresolved_residual",
                    "source_role": role,
                    "statement": statement,
                }
            )
        for hypothesis in _strings(task_package.get("hypotheses")):
            hypotheses.append(
                {
                    "kind": "candidate_hypothesis",
                    "source_role": role,
                    "statement": hypothesis,
                    "status": "untested",
                }
            )
            counterfactuals.append(_counterfactual(role, hypothesis))
        stop_point = str(task_package.get("stop_point") or "").strip()
        if stop_point:
            asymmetric_consequences.append(
                {
                    "kind": "role_stop_point_asymmetry",
                    "source_role": role,
                    "protected_boundary": stop_point,
                    "if_ignored": (
                        "The role could overrun its governed stop point and "
                        "invalidate downstream accountability."
                    ),
                }
            )
        for boundary in _strings(task_package.get("scope_boundary")):
            normalized = boundary.casefold()
            if "不要做" not in boundary and "do not" not in normalized:
                continue
            asymmetric_consequences.append(
                {
                    "kind": "boundary_asymmetry",
                    "source_role": role,
                    "protected_boundary": boundary,
                    "if_ignored": (
                        "The role's independence or the resulting conclusion "
                        "may become invalid."
                    ),
                }
            )
        for next_owner in _strings(task_package.get("next_possible_handoff")):
            next_candidate_moves.append(
                {
                    "kind": "candidate_handoff",
                    "from_role": role,
                    "to_role": next_owner,
                }
            )

    return {
        "knowns": _deduplicate_semantics(knowns),
        "unknowns": _deduplicate_semantics(unknowns),
        "competing_hypotheses": _deduplicate_semantics(hypotheses),
        "counterfactuals": _deduplicate_semantics(counterfactuals),
        "asymmetric_consequences": _deduplicate_semantics(
            asymmetric_consequences
        ),
        "knowledge_refs": list(capability_plan.get("knowledge_packs") or ()),
        "next_candidate_moves": _deduplicate_semantics(
            next_candidate_moves
        ),
        "current_judgment": {
            "status": "unformed",
            "statement": "",
        },
    }


def derive_execution_working_set_append(
    *,
    run_id: str,
    node_id: str,
    node_kind: str,
    governance_role: str,
    node_status: str,
    output: dict[str, Any],
) -> dict[str, list[Any]]:
    """Derive compact semantic revisions without inventing business findings."""

    patch: dict[str, list[Any]] = {
        "knowns": [],
        "unknowns": [],
        "competing_hypotheses": [],
        "counterfactuals": [],
        "asymmetric_consequences": [],
        "next_candidate_moves": [],
    }
    result_status = str(output.get("status") or node_status)
    if node_status == "succeeded" and node_kind not in {"human_gate", "assurance"}:
        observation = {
            "kind": "runtime_execution_fact",
            "run_id": run_id,
            "node_id": node_id,
            "node_kind": node_kind,
            "governance_role": governance_role,
            "result_status": result_status,
        }
        for field_name in (
            "route_id",
            "effective_route_id",
            "output_count",
            "verification_result",
        ):
            if output.get(field_name) is not None:
                observation[field_name] = output[field_name]
        patch["knowns"].append(observation)

    outcome = normalize_runtime_outcome(output)
    if outcome["execution_status"] != "succeeded":
        patch["asymmetric_consequences"].append(
            {
                "kind": "blocked_progression_asymmetry",
                "run_id": run_id,
                "node_id": node_id,
                "source_role": governance_role,
                "condition": outcome["source_status"],
                "blocking_reasons": outcome["blocking_reasons"],
                "if_ignored": (
                    "Downstream assurance or closure could overstate the "
                    "available evidence."
                ),
            }
        )

    return {key: value for key, value in patch.items() if value}
