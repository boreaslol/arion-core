"""Versioned Task and Working Set storage for Arion judgment continuity."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-posix fallback
    fcntl = None


TASK_SCHEMA_VERSION = "1.0"
WORKING_SET_SCHEMA_VERSION = "1.0"
TASK_STATUSES = {"active", "waiting", "closed"}
WORKING_SET_LIST_FIELDS = (
    "observed_experience",
    "knowns",
    "unknowns",
    "assumptions",
    "competing_hypotheses",
    "counterfactuals",
    "asymmetric_consequences",
    "active_obligations",
    "revision_triggers",
    "artifact_refs",
    "knowledge_refs",
    "next_candidate_moves",
)
WORKING_SET_SETTABLE_FIELDS = {*WORKING_SET_LIST_FIELDS, "current_judgment"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_task_id(objective: str) -> str:
    normalized = " ".join(objective.casefold().split())
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:48] or "arion-task"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"task-{slug}-{digest}"


def _storage_key(task_id: str) -> str:
    normalized = task_id.strip()
    if not normalized:
        raise ValueError("task_id is required")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", normalized).strip("-._")[:64] or "task"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _list_value(value: Any, *, field_name: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be a list")
    return _json_clone(list(value))


def _dedupe_json(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    deduped: list[Any] = []
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped


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


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    objective: str
    user_intent: str
    acceptance_boundary: str
    current_working_set_version: int
    status: str
    accountable_owner: str
    created_at: str
    updated_at: str
    schema_version: str = TASK_SCHEMA_VERSION

    def validate(self) -> list[str]:
        issues: list[str] = []
        if self.schema_version != TASK_SCHEMA_VERSION:
            issues.append(f"task schema_version must be {TASK_SCHEMA_VERSION}")
        for field_name in (
            "task_id",
            "objective",
            "user_intent",
            "acceptance_boundary",
            "accountable_owner",
            "created_at",
            "updated_at",
        ):
            if not str(getattr(self, field_name)).strip():
                issues.append(f"{field_name} is required")
        if self.current_working_set_version < 1:
            issues.append("current_working_set_version must be positive")
        if self.status not in TASK_STATUSES:
            issues.append(f"unsupported task status: {self.status}")
        return issues

    def require_valid(self) -> None:
        issues = self.validate()
        if issues:
            raise ValueError("invalid task record: " + "; ".join(issues))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskRecord":
        record = cls(
            task_id=str(payload.get("task_id") or ""),
            objective=str(payload.get("objective") or ""),
            user_intent=str(payload.get("user_intent") or ""),
            acceptance_boundary=str(payload.get("acceptance_boundary") or ""),
            current_working_set_version=int(
                payload.get("current_working_set_version") or 0
            ),
            status=str(payload.get("status") or ""),
            accountable_owner=str(payload.get("accountable_owner") or ""),
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            schema_version=str(payload.get("schema_version") or ""),
        )
        record.require_valid()
        return record


@dataclass(frozen=True)
class WorkingSetSnapshot:
    task_id: str
    version: int
    based_on_version: int | None
    created_at: str
    revised_by: str
    source_run_id: str | None
    revision_reason: str
    observed_experience: tuple[Any, ...] = ()
    knowns: tuple[Any, ...] = ()
    unknowns: tuple[Any, ...] = ()
    assumptions: tuple[Any, ...] = ()
    competing_hypotheses: tuple[Any, ...] = ()
    counterfactuals: tuple[Any, ...] = ()
    asymmetric_consequences: tuple[Any, ...] = ()
    current_judgment: dict[str, Any] = field(default_factory=dict)
    active_obligations: tuple[Any, ...] = ()
    revision_triggers: tuple[Any, ...] = ()
    artifact_refs: tuple[Any, ...] = ()
    knowledge_refs: tuple[Any, ...] = ()
    next_candidate_moves: tuple[Any, ...] = ()
    schema_version: str = WORKING_SET_SCHEMA_VERSION

    def validate(self) -> list[str]:
        issues: list[str] = []
        if self.schema_version != WORKING_SET_SCHEMA_VERSION:
            issues.append(
                f"working set schema_version must be {WORKING_SET_SCHEMA_VERSION}"
            )
        if not self.task_id.strip():
            issues.append("task_id is required")
        if self.version < 1:
            issues.append("version must be positive")
        if self.version == 1 and self.based_on_version is not None:
            issues.append("initial working set must not have based_on_version")
        if self.version > 1 and self.based_on_version != self.version - 1:
            issues.append("working set versions must form a contiguous chain")
        for field_name in ("created_at", "revised_by", "revision_reason"):
            if not str(getattr(self, field_name)).strip():
                issues.append(f"{field_name} is required")
        if not isinstance(self.current_judgment, dict):
            issues.append("current_judgment must be a mapping")
        return issues

    def require_valid(self) -> None:
        issues = self.validate()
        if issues:
            raise ValueError("invalid working set: " + "; ".join(issues))
        json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for field_name in WORKING_SET_LIST_FIELDS:
            payload[field_name] = list(payload[field_name])
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkingSetSnapshot":
        values = {
            field_name: tuple(
                _list_value(payload.get(field_name), field_name=field_name)
            )
            for field_name in WORKING_SET_LIST_FIELDS
        }
        snapshot = cls(
            task_id=str(payload.get("task_id") or ""),
            version=int(payload.get("version") or 0),
            based_on_version=(
                int(payload["based_on_version"])
                if payload.get("based_on_version") is not None
                else None
            ),
            created_at=str(payload.get("created_at") or ""),
            revised_by=str(payload.get("revised_by") or ""),
            source_run_id=(
                str(payload["source_run_id"])
                if payload.get("source_run_id") is not None
                else None
            ),
            revision_reason=str(payload.get("revision_reason") or ""),
            current_judgment=dict(payload.get("current_judgment") or {}),
            schema_version=str(payload.get("schema_version") or ""),
            **values,
        )
        snapshot.require_valid()
        return snapshot


class WorkingSetConflictError(RuntimeError):
    """Raised when a run tries to revise a stale Working Set version."""


class WorkingSetStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def _task_dir(self, task_id: str) -> Path:
        return self.root / _storage_key(task_id)

    def _task_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "task.json"

    def _working_set_path(self, task_id: str, version: int) -> Path:
        return self._task_dir(task_id) / "working_sets" / f"{version:08d}.json"

    @contextmanager
    def _locked_task(self, task_id: str) -> Iterator[None]:
        lock_path = self._task_dir(task_id) / ".working_set.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def create_task(
        self,
        *,
        task_id: str,
        objective: str,
        user_intent: str,
        acceptance_boundary: str,
        accountable_owner: str,
        initial_working_set: dict[str, Any] | None = None,
    ) -> tuple[TaskRecord, WorkingSetSnapshot]:
        with self._locked_task(task_id):
            task_path = self._task_path(task_id)
            if task_path.exists():
                raise FileExistsError(f"task already exists: {task_id}")
            now = utc_now()
            initial = dict(initial_working_set or {})
            unknown_fields = set(initial) - WORKING_SET_SETTABLE_FIELDS
            if unknown_fields:
                raise ValueError(
                    "unsupported initial working set fields: "
                    + ", ".join(sorted(unknown_fields))
                )
            values = {
                field_name: tuple(
                    _list_value(initial.get(field_name), field_name=field_name)
                )
                for field_name in WORKING_SET_LIST_FIELDS
            }
            snapshot = WorkingSetSnapshot(
                task_id=task_id,
                version=1,
                based_on_version=None,
                created_at=now,
                revised_by=accountable_owner,
                source_run_id=None,
                revision_reason="task_created",
                current_judgment=dict(initial.get("current_judgment") or {}),
                **values,
            )
            snapshot.require_valid()
            task = TaskRecord(
                task_id=task_id,
                objective=objective,
                user_intent=user_intent,
                acceptance_boundary=acceptance_boundary,
                current_working_set_version=1,
                status="active",
                accountable_owner=accountable_owner,
                created_at=now,
                updated_at=now,
            )
            task.require_valid()
            _atomic_write_json(self._working_set_path(task_id, 1), snapshot.to_dict())
            _atomic_write_json(task_path, task.to_dict())
            return task, snapshot

    def ensure_task(
        self,
        *,
        task_id: str,
        objective: str,
        user_intent: str,
        acceptance_boundary: str,
        accountable_owner: str,
        initial_working_set: dict[str, Any] | None = None,
    ) -> tuple[TaskRecord, WorkingSetSnapshot]:
        try:
            return self.load_context(task_id)
        except FileNotFoundError:
            try:
                return self.create_task(
                    task_id=task_id,
                    objective=objective,
                    user_intent=user_intent,
                    acceptance_boundary=acceptance_boundary,
                    accountable_owner=accountable_owner,
                    initial_working_set=initial_working_set,
                )
            except FileExistsError:
                return self.load_context(task_id)

    def load_task(self, task_id: str) -> TaskRecord:
        path = self._task_path(task_id)
        if not path.is_file():
            raise FileNotFoundError(f"task does not exist: {task_id}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"task record is not a mapping: {path}")
        task = TaskRecord.from_dict(payload)
        if task.task_id != task_id:
            raise ValueError(f"task identity mismatch in {path}")
        return task

    def load_working_set(
        self,
        task_id: str,
        version: int | None = None,
    ) -> WorkingSetSnapshot:
        task = self.load_task(task_id)
        selected_version = version or task.current_working_set_version
        path = self._working_set_path(task_id, selected_version)
        if not path.is_file():
            raise FileNotFoundError(
                f"working set version does not exist: {task_id}@{selected_version}"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"working set is not a mapping: {path}")
        snapshot = WorkingSetSnapshot.from_dict(payload)
        if snapshot.task_id != task_id or snapshot.version != selected_version:
            raise ValueError(f"working set identity mismatch in {path}")
        return snapshot

    def load_context(self, task_id: str) -> tuple[TaskRecord, WorkingSetSnapshot]:
        task = self.load_task(task_id)
        return task, self.load_working_set(
            task_id,
            version=task.current_working_set_version,
        )

    def update_working_set(
        self,
        *,
        task_id: str,
        expected_version: int,
        revised_by: str,
        revision_reason: str,
        source_run_id: str | None = None,
        set_fields: dict[str, Any] | None = None,
        append_fields: dict[str, list[Any] | tuple[Any, ...]] | None = None,
        task_status: str | None = None,
    ) -> tuple[TaskRecord, WorkingSetSnapshot]:
        set_values = dict(set_fields or {})
        append_values = dict(append_fields or {})
        unknown_set = set(set_values) - WORKING_SET_SETTABLE_FIELDS
        unknown_append = set(append_values) - set(WORKING_SET_LIST_FIELDS)
        if unknown_set:
            raise ValueError(
                "unsupported Working Set set fields: "
                + ", ".join(sorted(unknown_set))
            )
        if unknown_append:
            raise ValueError(
                "unsupported Working Set append fields: "
                + ", ".join(sorted(unknown_append))
            )
        if task_status is not None and task_status not in TASK_STATUSES:
            raise ValueError(f"unsupported task status: {task_status}")

        with self._locked_task(task_id):
            task = self.load_task(task_id)
            if task.current_working_set_version != expected_version:
                raise WorkingSetConflictError(
                    "working set changed before update: "
                    f"expected {expected_version}, "
                    f"current {task.current_working_set_version}"
                )
            current = self.load_working_set(task_id, version=expected_version)
            payload = current.to_dict()
            for field_name, value in set_values.items():
                if field_name in WORKING_SET_LIST_FIELDS:
                    if not isinstance(value, (list, tuple)):
                        raise ValueError(f"{field_name} must be a list")
                    payload[field_name] = _json_clone(list(value))
                else:
                    if not isinstance(value, dict):
                        raise ValueError("current_judgment must be a mapping")
                    payload[field_name] = _json_clone(value)
            for field_name, values in append_values.items():
                if not isinstance(values, (list, tuple)):
                    raise ValueError(f"append field {field_name} must be a list")
                payload[field_name] = _dedupe_json(
                    [*payload.get(field_name, []), *_json_clone(list(values))]
                )

            new_version = expected_version + 1
            snapshot = WorkingSetSnapshot(
                task_id=task_id,
                version=new_version,
                based_on_version=expected_version,
                created_at=utc_now(),
                revised_by=revised_by,
                source_run_id=source_run_id,
                revision_reason=revision_reason,
                current_judgment=dict(payload.get("current_judgment") or {}),
                **{
                    field_name: tuple(payload.get(field_name) or ())
                    for field_name in WORKING_SET_LIST_FIELDS
                },
            )
            snapshot.require_valid()
            updated_task = TaskRecord(
                task_id=task.task_id,
                objective=task.objective,
                user_intent=task.user_intent,
                acceptance_boundary=task.acceptance_boundary,
                current_working_set_version=new_version,
                status=task_status or task.status,
                accountable_owner=task.accountable_owner,
                created_at=task.created_at,
                updated_at=utc_now(),
            )
            updated_task.require_valid()
            _atomic_write_json(
                self._working_set_path(task_id, new_version),
                snapshot.to_dict(),
            )
            _atomic_write_json(self._task_path(task_id), updated_task.to_dict())
            return updated_task, snapshot
