"""Canonical bounded route contracts for the Arion runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from planner.intent_utils import detect_alias_groups, normalize_text


@dataclass(frozen=True)
class RouteSpec:
    route_id: str
    intent_family: str
    summary: str
    schema_family: str
    default_window_months: int
    confidence: float
    query_families: tuple[str, ...]
    required_preconditions: tuple[str, ...]
    next_actions: tuple[str, ...]
    match_any: tuple[str, ...]
    match_detected_entity: bool
    priority: int


@dataclass(frozen=True)
class RouteDecision:
    question: str
    route_id: str
    intent_family: str
    summary: str
    schema_family: str
    status: str
    confidence: float
    matched_signals: tuple[str, ...]
    detected_entities: tuple[str, ...]
    detected_dimensions: tuple[str, ...]
    query_families: tuple[str, ...]
    default_window_months: int
    required_preconditions: tuple[str, ...]
    blocked_reason: str | None
    next_actions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_contract(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"expected route contract mapping: {path}")
    return payload


@lru_cache(maxsize=4)
def load_route_contracts(
    path: str | Path,
) -> dict[str, RouteSpec]:
    payload = _read_contract(Path(path))
    routes: dict[str, RouteSpec] = {}
    for item in payload.get("routes", []) or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        route_id = str(item["id"])
        routes[route_id] = RouteSpec(
            route_id=route_id,
            intent_family=str(item.get("intent_family") or route_id),
            summary=str(item.get("summary") or route_id),
            schema_family=str(item.get("schema_family") or "analytical"),
            default_window_months=int(item.get("default_window_months") or 4),
            confidence=float(item.get("confidence") or 0.5),
            query_families=tuple(
                str(value) for value in item.get("query_families", []) or []
            ),
            required_preconditions=tuple(
                str(value)
                for value in item.get("required_preconditions", []) or []
            ),
            next_actions=tuple(
                str(value) for value in item.get("next_actions", []) or []
            ),
            match_any=tuple(
                str(value) for value in item.get("match_any", []) or []
            ),
            match_detected_entity=bool(item.get("match_detected_entity")),
            priority=int(item.get("priority") or 100),
        )
    if not routes:
        raise ValueError(f"route contract contains no routes: {path}")
    return routes


def _dimension_aliases(path: Path) -> dict[str, tuple[str, ...]]:
    payload = _read_contract(path)
    return {
        str(dimension): tuple(str(value) for value in aliases or [])
        for dimension, aliases in (payload.get("dimensions") or {}).items()
    }


def _entity_aliases(path: Path) -> dict[str, tuple[str, ...]]:
    payload = _read_contract(path)
    return {
        str(entity): tuple(str(value) for value in aliases or [])
        for entity, aliases in (payload.get("entity_aliases") or {}).items()
    }


def detect_dimensions(
    question: str,
    *,
    path: str | Path,
) -> tuple[str, ...]:
    normalized = normalize_text(question)
    detected = []
    for dimension, aliases in _dimension_aliases(Path(path)).items():
        if any(normalize_text(alias) in normalized for alias in aliases):
            detected.append(dimension)
    return tuple(detected)


def build_route_decision(
    question: str,
    *,
    path: str | Path,
) -> RouteDecision:
    contract_path = Path(path)
    payload = _read_contract(contract_path)
    routes = load_route_contracts(contract_path)
    normalized = normalize_text(question)
    entities = detect_alias_groups(
        question,
        _entity_aliases(contract_path),
    )
    selected: RouteSpec | None = None
    matched_signals: tuple[str, ...] = ()
    for route in sorted(routes.values(), key=lambda item: item.priority):
        matched = tuple(
            term
            for term in route.match_any
            if normalize_text(term) in normalized
        )
        if not matched and route.match_detected_entity and entities:
            matched = ("detected_entity",)
        if matched:
            selected = route
            matched_signals = matched
            break
    if selected is None:
        default_route = str(payload.get("default_route") or "").strip()
        if not default_route:
            raise ValueError("route contract must declare default_route")
        try:
            selected = routes[default_route]
        except KeyError as exc:
            raise ValueError(
                f"route contract default_route is not declared: {default_route}"
            ) from exc
        matched_signals = ("bounded_default",)
    return RouteDecision(
        question=question,
        route_id=selected.route_id,
        intent_family=selected.intent_family,
        summary=selected.summary,
        schema_family=selected.schema_family,
        status="active",
        confidence=selected.confidence,
        matched_signals=matched_signals,
        detected_entities=entities,
        detected_dimensions=detect_dimensions(question, path=contract_path),
        query_families=selected.query_families,
        default_window_months=selected.default_window_months,
        required_preconditions=selected.required_preconditions,
        blocked_reason=None,
        next_actions=selected.next_actions,
    )
