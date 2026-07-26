"""Resolve Arion runtime storage outside the source repository."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARION_HOME_ENV = "ARION_HOME"


def _expanded_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def default_arion_home(
    *,
    environ: Mapping[str, str] | None = None,
    user_home: str | Path | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    configured = str(values.get(ARION_HOME_ENV) or "").strip()
    if configured:
        return _expanded_path(configured)
    xdg_data_home = str(values.get("XDG_DATA_HOME") or "").strip()
    if xdg_data_home:
        return _expanded_path(xdg_data_home) / "arion"
    home = _expanded_path(user_home or Path.home())
    return home / ".local" / "share" / "arion"


def resolve_arion_home(
    value: str | Path | None = None,
    *,
    project_root: str | Path = PROJECT_ROOT,
    environ: Mapping[str, str] | None = None,
    user_home: str | Path | None = None,
) -> Path:
    resolved = (
        _expanded_path(value)
        if value is not None
        else default_arion_home(environ=environ, user_home=user_home)
    )
    repository = _expanded_path(project_root)
    if resolved == repository or resolved.is_relative_to(repository):
        raise ValueError(
            "ARION_HOME must be outside the source repository; "
            f"got {resolved}"
        )
    return resolved


def require_external_runtime_path(
    value: str | Path,
    *,
    project_root: str | Path = PROJECT_ROOT,
    purpose: str = "runtime payload",
) -> Path:
    resolved = _expanded_path(value)
    repository = _expanded_path(project_root)
    if resolved == repository or resolved.is_relative_to(repository):
        raise ValueError(
            f"{purpose} must be outside the source repository; got {resolved}"
        )
    return resolved


@dataclass(frozen=True)
class RuntimePaths:
    home: Path
    tasks: Path
    runs: Path
    artifacts: Path
    resources: Path
    models: Path
    datasets: Path
    cache: Path
    worktrees: Path
    state: Path
    backups: Path

    @property
    def runtime_state_db(self) -> Path:
        return self.state / "runtime_v1.sqlite3"

    @property
    def runtime_backup_root(self) -> Path:
        return self.backups / "runtime_v1"

    def ensure(self) -> "RuntimePaths":
        for path in asdict(self).values():
            Path(path).mkdir(parents=True, exist_ok=True)
        return self

    def to_dict(self) -> dict[str, str]:
        payload = {key: str(value) for key, value in asdict(self).items()}
        payload["runtime_state_db"] = str(self.runtime_state_db)
        payload["runtime_backup_root"] = str(self.runtime_backup_root)
        return payload


def build_runtime_paths(
    arion_home: str | Path | None = None,
    *,
    project_root: str | Path = PROJECT_ROOT,
    environ: Mapping[str, str] | None = None,
    user_home: str | Path | None = None,
    create: bool = False,
) -> RuntimePaths:
    home = resolve_arion_home(
        arion_home,
        project_root=project_root,
        environ=environ,
        user_home=user_home,
    )
    paths = RuntimePaths(
        home=home,
        tasks=home / "tasks",
        runs=home / "runs",
        artifacts=home / "artifacts",
        resources=home / "resources",
        models=home / "models",
        datasets=home / "datasets",
        cache=home / "cache",
        worktrees=home / "worktrees",
        state=home / "state",
        backups=home / "backups",
    )
    return paths.ensure() if create else paths


def resolve_portable_reference(
    reference: str | Path,
    *,
    project_root: str | Path = PROJECT_ROOT,
    arion_home: str | Path | None = None,
    base_dir: str | Path | None = None,
) -> Path:
    """Resolve repository-relative and ``$ARION_HOME`` references.

    Relative references resolve from ``base_dir`` when provided, while still
    remaining inside ``project_root``.
    """

    value = str(reference).strip()
    resolved_project_root = _expanded_path(project_root)
    if value == "$ARION_HOME" or value.startswith("$ARION_HOME/"):
        runtime_home = build_runtime_paths(
            arion_home,
            project_root=resolved_project_root,
        ).home
        suffix = value.removeprefix("$ARION_HOME").lstrip("/")
        resolved = (runtime_home / suffix).resolve() if suffix else runtime_home
        if not resolved.is_relative_to(runtime_home):
            raise ValueError(f"Reference escapes ARION_HOME: {value}")
        return resolved

    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    resolved_base_dir = (
        _expanded_path(base_dir)
        if base_dir is not None
        else resolved_project_root
    )
    if not resolved_base_dir.is_relative_to(resolved_project_root):
        raise ValueError(
            "Portable reference base directory must be inside the project root: "
            f"{resolved_base_dir}"
        )
    resolved = (resolved_base_dir / path).resolve()
    if not resolved.is_relative_to(resolved_project_root):
        raise ValueError(f"Reference escapes project root: {value}")
    return resolved
