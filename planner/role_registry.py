"""Load the canonical fixed governance-role registry."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class GovernanceRegistry:
    registry_reference: str
    role_order: tuple[str, ...]
    orchestrator_role: str
    role_output_files: dict[str, str]

    @property
    def roles(self) -> frozenset[str]:
        return frozenset(self.role_order)

    @property
    def execution_roles(self) -> frozenset[str]:
        return self.roles - {self.orchestrator_role}


@lru_cache(maxsize=4)
def load_role_registry(path: str | Path) -> dict[str, Any]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"expected role registry mapping: {path}")
    roles = payload.get("roles")
    if not isinstance(roles, list) or not roles:
        raise ValueError(f"role registry must contain a non-empty roles list: {path}")
    return payload


def compile_governance_registry(
    payload: dict[str, Any],
    *,
    registry_reference: str,
) -> GovernanceRegistry:
    roles = payload.get("roles")
    if not isinstance(roles, list) or not roles:
        raise ValueError("role registry must contain a non-empty roles list")
    names: list[str] = []
    role_output_files: dict[str, str] = {}
    for index, role in enumerate(roles):
        if not isinstance(role, dict):
            raise ValueError(f"role registry entry {index} must be a mapping")
        name = str(role.get("name", "")).strip()
        if not name:
            raise ValueError(f"role registry entry {index} must define name")
        if role.get("lifecycle_phase") != "active" or role.get("routable") is not True:
            raise ValueError(f"governed role must be active and routable: {name}")
        names.append(name)
        output_file = str(
            ((role.get("asset_readiness") or {}).get("run_contract") or {}).get(
                "output_file"
            )
            or ""
        ).strip()
        if not output_file:
            raise ValueError(f"governed role must define output_file: {name}")
        output_path = Path(output_file)
        if output_path.is_absolute() or ".." in output_path.parts:
            raise ValueError(
                f"governed role output_file must be run-relative: {name}"
            )
        role_output_files[name] = output_file
    if len(names) != len(set(names)):
        raise ValueError("role registry contains duplicate governed role names")
    if len(role_output_files.values()) != len(set(role_output_files.values())):
        raise ValueError("role registry contains duplicate governed output files")
    runtime_activation = payload.get("runtime_activation") or {}
    orchestrator_role = str(
        runtime_activation.get("orchestrator_role") or ""
    ).strip()
    if not orchestrator_role:
        raise ValueError("role registry must define runtime orchestrator_role")
    if names[0] != orchestrator_role:
        raise ValueError("orchestrator_role must remain the first governed role")
    return GovernanceRegistry(
        registry_reference=registry_reference,
        role_order=tuple(names),
        orchestrator_role=orchestrator_role,
        role_output_files=role_output_files,
    )


def compile_governed_role_order(payload: dict[str, Any]) -> tuple[str, ...]:
    return compile_governance_registry(
        payload,
        registry_reference="<in-memory>",
    ).role_order
