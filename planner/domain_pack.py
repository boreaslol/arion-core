"""Explicit, immutable Domain Pack loading for the Arion core."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from planner.role_registry import (
    GovernanceRegistry,
    compile_governance_registry,
    load_role_registry,
)


DOMAIN_PACK_SCHEMA_VERSION = "1.0"
REQUIRED_MANIFEST_PATH_FIELDS = (
    "role_registry",
    "runtime_policy",
    "role_contracts",
    "route_contracts",
    "executor_bindings",
)
OPTIONAL_MANIFEST_FILE_FIELDS = ("golden_workflows",)
OPTIONAL_MANIFEST_ROOT_FIELDS = (
    "semantic_roots",
    "knowledge_roots",
    "capability_roots",
    "resource_adapters",
    "action_protocol_roots",
)
OPTIONAL_MANIFEST_DOCUMENT_FIELDS = ("reference_documents",)
DOMAIN_PACK_IDENTITY_FIELDS = (
    "schema_version",
    "pack_id",
    "pack_version",
    "manifest",
)


def _read_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"expected Domain Pack mapping: {path}")
    return payload


def _inside_root(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Domain Pack path escapes pack root: {path}") from exc
    return resolved


@dataclass(frozen=True)
class DomainPack:
    root: Path
    manifest_path: Path
    pack_id: str
    pack_version: str
    manifest: dict[str, Any]
    governance: GovernanceRegistry
    paths: dict[str, Path]
    path_groups: dict[str, tuple[Path, ...]]

    def path(self, field: str) -> Path:
        try:
            return self.paths[field]
        except KeyError as exc:
            raise KeyError(f"Domain Pack does not define path field: {field}") from exc

    @property
    def identity(self) -> dict[str, str]:
        return {
            "schema_version": DOMAIN_PACK_SCHEMA_VERSION,
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "manifest": self.manifest_path.relative_to(self.root).as_posix(),
        }

    def runtime_metadata(self) -> dict[str, Any]:
        return {
            "domain_pack": self.identity,
            "governance_roles": list(self.governance.role_order),
            "orchestrator_role": self.governance.orchestrator_role,
            "role_output_files": dict(self.governance.role_output_files),
        }

    def paths_for(self, field: str) -> tuple[Path, ...]:
        return self.path_groups.get(field, ())

    @property
    def golden_workflows_path(self) -> Path:
        return self.path("golden_workflows")


def validate_domain_pack_identity(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["domain_pack identity is required"]
    issues: list[str] = []
    for field in DOMAIN_PACK_IDENTITY_FIELDS:
        if not isinstance(value.get(field), str) or not value[field].strip():
            issues.append(f"domain_pack.{field} is required")
    if (
        str(value.get("schema_version") or "").strip()
        and value.get("schema_version") != DOMAIN_PACK_SCHEMA_VERSION
    ):
        issues.append(
            "domain_pack.schema_version must be "
            f"{DOMAIN_PACK_SCHEMA_VERSION}"
        )
    manifest = Path(str(value.get("manifest") or ""))
    if manifest.is_absolute() or ".." in manifest.parts:
        issues.append("domain_pack.manifest must be a root-relative path")
    return issues


def _resolve_manifest_path(
    *,
    root: Path,
    field: str,
    raw_value: Any,
) -> Path:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ValueError(f"Domain Pack {field} must be a relative path string")
    relative = Path(raw_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Domain Pack {field} must be a root-relative path")
    return _inside_root(root, root / relative)


def load_domain_pack(
    *,
    pack_root: str | Path,
    manifest_path: str | Path,
) -> DomainPack:
    root = Path(pack_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Domain Pack root is not a directory: {root}")
    raw_manifest_path = Path(manifest_path).expanduser()
    if not raw_manifest_path.is_absolute():
        raw_manifest_path = root / raw_manifest_path
    resolved_manifest = _inside_root(root, raw_manifest_path)
    if not resolved_manifest.is_file():
        raise ValueError(f"Domain Pack manifest is missing: {resolved_manifest}")
    manifest = _read_yaml(resolved_manifest)
    if manifest.get("schema_version") != DOMAIN_PACK_SCHEMA_VERSION:
        raise ValueError(
            "Domain Pack schema_version must be "
            f"{DOMAIN_PACK_SCHEMA_VERSION}"
        )
    pack_id = str(manifest.get("pack_id") or "").strip()
    pack_version = str(manifest.get("pack_version") or "").strip()
    if not pack_id:
        raise ValueError("Domain Pack pack_id is required")
    if not pack_version:
        raise ValueError("Domain Pack pack_version is required")

    paths: dict[str, Path] = {}
    for field in (*REQUIRED_MANIFEST_PATH_FIELDS, *OPTIONAL_MANIFEST_FILE_FIELDS):
        raw_value = manifest.get(field)
        if raw_value is None or raw_value == "":
            if field in REQUIRED_MANIFEST_PATH_FIELDS:
                raise ValueError(f"Domain Pack {field} is required")
            continue
        resolved = _resolve_manifest_path(
            root=root,
            field=field,
            raw_value=raw_value,
        )
        if not resolved.is_file():
            raise ValueError(f"Domain Pack {field} is missing: {resolved}")
        paths[field] = resolved

    path_groups: dict[str, tuple[Path, ...]] = {}
    for field in (
        *OPTIONAL_MANIFEST_ROOT_FIELDS,
        *OPTIONAL_MANIFEST_DOCUMENT_FIELDS,
    ):
        raw_values = manifest.get(field)
        if raw_values is None:
            continue
        if not isinstance(raw_values, list):
            raise ValueError(f"Domain Pack {field} must be a list")
        resolved_values: list[Path] = []
        for index, raw_value in enumerate(raw_values):
            resolved = _resolve_manifest_path(
                root=root,
                field=f"{field}[{index}]",
                raw_value=raw_value,
            )
            if field in OPTIONAL_MANIFEST_ROOT_FIELDS and not resolved.is_dir():
                raise ValueError(
                    f"Domain Pack {field}[{index}] is not a directory: {resolved}"
                )
            if (
                field in OPTIONAL_MANIFEST_DOCUMENT_FIELDS
                and not resolved.is_file()
            ):
                raise ValueError(
                    f"Domain Pack {field}[{index}] is not a file: {resolved}"
                )
            resolved_values.append(resolved)
        path_groups[field] = tuple(resolved_values)

    governance = compile_governance_registry(
        load_role_registry(paths["role_registry"]),
        registry_reference=str(manifest["role_registry"]),
    )
    return DomainPack(
        root=root,
        manifest_path=resolved_manifest,
        pack_id=pack_id,
        pack_version=pack_version,
        manifest=manifest,
        governance=governance,
        paths=paths,
        path_groups=path_groups,
    )
