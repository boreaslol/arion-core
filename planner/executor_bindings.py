"""Build an executor registry from one explicitly selected Domain Pack."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from typing import Any

import yaml

from planner.domain_pack import DomainPack
from planner.unified_execution import Executor, ExecutorRegistry


EXECUTOR_BINDINGS_VERSION = 1
RUNTIME_KINDS = {"direct_run", "task_graph"}
MATCH_KINDS = {"exact", "prefix"}
EXECUTOR_BINDING_FIELDS = {"version", "status", "registrations"}


@dataclass(frozen=True)
class ExecutorBinding:
    runtime_kind: str
    match: str
    executor_id: str
    callable_ref: str


def _load_callable(reference: str) -> Executor:
    module_name, separator, attribute_path = reference.partition(":")
    if not separator or not module_name.strip() or not attribute_path.strip():
        raise ValueError(
            "executor callable must use module.path:attribute syntax: "
            f"{reference}"
        )
    try:
        target: Any = importlib.import_module(module_name)
        for attribute in attribute_path.split("."):
            target = getattr(target, attribute)
    except (ImportError, AttributeError) as exc:
        raise ValueError(
            f"executor binding callable cannot be loaded: {reference}"
        ) from exc
    if not callable(target):
        raise ValueError(f"executor binding target is not callable: {reference}")
    return target


def load_executor_bindings(
    domain_pack: DomainPack,
    *,
    runtime_kind: str,
) -> tuple[ExecutorBinding, ...]:
    if runtime_kind not in RUNTIME_KINDS:
        raise ValueError(f"unsupported executor runtime kind: {runtime_kind}")
    path = domain_pack.path("executor_bindings")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"expected executor binding mapping: {path}")
    unknown_fields = set(payload) - EXECUTOR_BINDING_FIELDS
    if unknown_fields:
        raise ValueError(
            "executor bindings contain unsupported top-level fields: "
            + ", ".join(sorted(unknown_fields))
        )
    if payload.get("version") != EXECUTOR_BINDINGS_VERSION:
        raise ValueError(
            "executor binding version must be "
            f"{EXECUTOR_BINDINGS_VERSION}"
        )
    registrations = payload.get("registrations") or {}
    if not isinstance(registrations, dict):
        raise ValueError("executor bindings registrations must be a mapping")
    raw_bindings = registrations.get(runtime_kind) or []
    if not isinstance(raw_bindings, list):
        raise ValueError(
            f"executor bindings registrations.{runtime_kind} must be a list"
        )

    bindings: list[ExecutorBinding] = []
    seen: set[tuple[str, str]] = set()
    for index, raw_binding in enumerate(raw_bindings):
        if not isinstance(raw_binding, dict):
            raise ValueError(
                f"executor binding {runtime_kind}[{index}] must be a mapping"
            )
        match = str(raw_binding.get("match") or "").strip()
        executor_id = str(raw_binding.get("executor_id") or "").strip()
        callable_ref = str(raw_binding.get("callable") or "").strip()
        if match not in MATCH_KINDS:
            raise ValueError(
                f"executor binding {runtime_kind}[{index}] has unsupported "
                f"match: {match}"
            )
        if not executor_id:
            raise ValueError(
                f"executor binding {runtime_kind}[{index}] requires executor_id"
            )
        if not callable_ref:
            raise ValueError(
                f"executor binding {runtime_kind}[{index}] requires callable"
            )
        identity = (match, executor_id)
        if identity in seen:
            raise ValueError(
                "executor bindings contain a duplicate registration: "
                f"{runtime_kind}:{match}:{executor_id}"
            )
        seen.add(identity)
        bindings.append(
            ExecutorBinding(
                runtime_kind=runtime_kind,
                match=match,
                executor_id=executor_id,
                callable_ref=callable_ref,
            )
        )
    return tuple(bindings)


def build_executor_registry(
    domain_pack: DomainPack,
    *,
    runtime_kind: str,
) -> ExecutorRegistry:
    registry = ExecutorRegistry(domain_pack_identity=domain_pack.identity)
    for binding in load_executor_bindings(
        domain_pack,
        runtime_kind=runtime_kind,
    ):
        executor = _load_callable(binding.callable_ref)
        if binding.match == "exact":
            registry.register(binding.executor_id, executor)
        else:
            registry.register_prefix(binding.executor_id, executor)
    return registry
