"""Normalize role-runtime execution and acceptance outcomes."""

from __future__ import annotations

from typing import Any


RUNTIME_OUTCOME_SCHEMA_VERSION = 1
RUNTIME_OUTCOME_REQUIRED_FIELDS = {
    "execution_status",
    "acceptance_status",
    "source_field",
    "source_status",
    "blocking_reasons",
}
ENGINE_EXECUTION_STATUSES = {"succeeded", "waiting_external", "failed"}
ACCEPTED_VERIFICATION_RESULTS = {
    "candidate_quality_evidence_compiled",
    "preflight_evidence_compiled",
    "runtime_evidence_compiled",
}
_WAITING_PREFIXES = ("blocked", "waiting", "unverified")
_FAILED_PREFIXES = ("failed", "error")
_PREPARED_STATUSES = {
    "implementation_packet_prepared",
    "prepared_not_released",
}


def _normalized_status(value: Any) -> str:
    return str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")


def _matches_prefix(value: str, prefixes: tuple[str, ...]) -> bool:
    return any(value == prefix or value.startswith(f"{prefix}_") for prefix in prefixes)


def _reason_list(payload: dict[str, Any], fallback: str) -> list[str]:
    reasons: list[str] = []
    for field_name in ("blocking_reasons", "unverified_items"):
        value = payload.get(field_name)
        if isinstance(value, (list, tuple)):
            reasons.extend(str(item).strip() for item in value if str(item).strip())
        elif value:
            reasons.append(str(value).strip())
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("type")
        if message:
            reasons.append(str(message).strip())
    elif error:
        reasons.append(str(error).strip())
    if not reasons and fallback:
        reasons.append(fallback)
    return list(dict.fromkeys(reasons))


def normalize_runtime_outcome(payload: dict[str, Any]) -> dict[str, Any]:
    """Return one canonical engine/acceptance outcome for a role payload."""

    explicit = payload.get("runtime_outcome")
    if isinstance(explicit, dict):
        execution_status = str(explicit.get("execution_status") or "")
        if execution_status in ENGINE_EXECUTION_STATUSES:
            return {
                "schema_version": RUNTIME_OUTCOME_SCHEMA_VERSION,
                "execution_status": execution_status,
                "acceptance_status": str(
                    explicit.get("acceptance_status") or "completed"
                ),
                "source_field": str(explicit.get("source_field") or "runtime_outcome"),
                "source_status": str(explicit.get("source_status") or execution_status),
                "blocking_reasons": _reason_list(
                    explicit,
                    str(explicit.get("source_status") or execution_status),
                )
                if execution_status != "succeeded"
                else [],
            }

    candidates = [
        ("verification_result", _normalized_status(payload.get("verification_result"))),
        ("acceptance_status", _normalized_status(payload.get("acceptance_status"))),
        ("status", _normalized_status(payload.get("status"))),
    ]
    source_field = "status"
    source_status = _normalized_status(payload.get("status")) or "succeeded"
    execution_status = "succeeded"

    for field_name, value in candidates:
        if value and _matches_prefix(value, _FAILED_PREFIXES):
            source_field = field_name
            source_status = value
            execution_status = "failed"
            break
    else:
        for field_name, value in candidates:
            if value and _matches_prefix(value, _WAITING_PREFIXES):
                source_field = field_name
                source_status = value
                execution_status = "waiting_external"
                break

    explicit_acceptance = _normalized_status(payload.get("acceptance_status"))
    verification_result = _normalized_status(payload.get("verification_result"))
    if explicit_acceptance:
        acceptance_status = explicit_acceptance
    elif execution_status == "failed":
        acceptance_status = "failed"
    elif execution_status == "waiting_external":
        acceptance_status = (
            "unverified"
            if _matches_prefix(source_status, ("unverified",))
            else "blocked"
        )
    elif verification_result in ACCEPTED_VERIFICATION_RESULTS:
        acceptance_status = "evidence_ready"
    elif source_status in _PREPARED_STATUSES:
        acceptance_status = "prepared"
    else:
        acceptance_status = "completed"

    return {
        "schema_version": RUNTIME_OUTCOME_SCHEMA_VERSION,
        "execution_status": execution_status,
        "acceptance_status": acceptance_status,
        "source_field": source_field,
        "source_status": source_status,
        "blocking_reasons": (
            _reason_list(payload, source_status)
            if execution_status != "succeeded"
            else []
        ),
    }


def enrich_runtime_output(payload: dict[str, Any]) -> dict[str, Any]:
    output = dict(payload)
    output["runtime_outcome"] = normalize_runtime_outcome(output)
    return output


def verifier_evidence_ready(payload: dict[str, Any]) -> bool:
    outcome = normalize_runtime_outcome(payload)
    return (
        outcome["execution_status"] == "succeeded"
        and _normalized_status(payload.get("verification_result"))
        in ACCEPTED_VERIFICATION_RESULTS
    )
