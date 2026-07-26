"""Local runtime backup, restore, and restore-drill verification."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any

from planner.unified_execution import sha256_file


CRITICAL_FILE_NAMES = {
    "task_graph.json",
    "planner_decision.json",
    "runtime_snapshot.json",
    "runtime_events.jsonl",
    "assurance_report.json",
    "workspace_environment_manifest.json",
}


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_connection, sqlite3.connect(target) as target_connection:
        source_connection.backup(target_connection)


def _critical_files(run_root: Path) -> list[Path]:
    if not run_root.exists():
        return []
    return sorted(
        path
        for path in run_root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and (
            path.name in CRITICAL_FILE_NAMES
            or path.name.endswith(".manifest.json")
            or "manifests" in path.parts
        )
    )


def create_runtime_backup(
    *,
    runtime_db: str | Path,
    run_root: str | Path,
    backup_root: str | Path,
    backup_id: str | None = None,
) -> Path:
    source_db = Path(runtime_db)
    if not source_db.is_file():
        raise FileNotFoundError(source_db)
    source_runs = Path(run_root)
    target = Path(backup_root) / (backup_id or f"runtime-backup-{_utc_stamp()}")
    if target.exists():
        raise FileExistsError(target)
    target.mkdir(parents=True)
    backup_db = target / "runtime.sqlite3"
    _sqlite_backup(source_db, backup_db)

    artifacts_root = target / "artifacts"
    files: list[dict[str, Any]] = []
    for source in _critical_files(source_runs):
        relative = source.relative_to(source_runs)
        destination = artifacts_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        files.append(
            {
                "relative_path": str(relative),
                "sha256": sha256_file(destination),
                "size": destination.stat().st_size,
            }
        )
    manifest = {
        "backup_version": 1,
        "backup_id": target.name,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runtime_db_sha256": sha256_file(backup_db),
        "artifact_count": len(files),
        "artifacts": files,
        "source_run_root": str(source_runs.resolve()),
        "local_only": True,
    }
    (target / "backup_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def _database_counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise ValueError(f"SQLite integrity check failed: {integrity}")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        return {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in ("runtime_runs", "runtime_nodes", "runtime_events", "runtime_artifacts")
            if table in tables
        }


def restore_runtime_backup(
    *,
    backup_dir: str | Path,
    restore_db: str | Path,
    restore_run_root: str | Path,
) -> dict[str, Any]:
    source = Path(backup_dir)
    manifest = json.loads((source / "backup_manifest.json").read_text(encoding="utf-8"))
    source_db = source / "runtime.sqlite3"
    if sha256_file(source_db) != manifest["runtime_db_sha256"]:
        raise ValueError("runtime backup database checksum mismatch")
    target_db = Path(restore_db)
    target_runs = Path(restore_run_root)
    if target_db.exists() or target_runs.exists():
        raise FileExistsError("restore targets must not already exist")
    target_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_db, target_db)
    target_runs.mkdir(parents=True)
    for artifact in manifest.get("artifacts", []):
        relative = Path(str(artifact["relative_path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe backup artifact path: {relative}")
        source_file = source / "artifacts" / relative
        if sha256_file(source_file) != artifact["sha256"]:
            raise ValueError(f"backup artifact checksum mismatch: {relative}")
        destination = target_runs / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination)
    restored_counts = _database_counts(target_db)
    return {
        "status": "pass",
        "restored_db": str(target_db),
        "restored_run_root": str(target_runs),
        "database_counts": restored_counts,
        "artifact_count": len(manifest.get("artifacts", [])),
    }


def run_restore_drill(backup_dir: str | Path) -> dict[str, Any]:
    source_db = Path(backup_dir) / "runtime.sqlite3"
    source_counts = _database_counts(source_db)
    with tempfile.TemporaryDirectory(prefix="arion-runtime-restore-") as temp_dir:
        root = Path(temp_dir)
        result = restore_runtime_backup(
            backup_dir=backup_dir,
            restore_db=root / "restored" / "runtime.sqlite3",
            restore_run_root=root / "restored-runs",
        )
        result["source_database_counts"] = source_counts
        result["counts_match"] = result["database_counts"] == source_counts
        result["status"] = "pass" if result["counts_match"] else "fail"
        return result
