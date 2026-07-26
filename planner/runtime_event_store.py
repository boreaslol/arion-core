"""SQLite event and state store for the Arion local runtime."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from planner.task_graph import TaskGraph, TaskNode


TERMINAL_NODE_STATUSES = {"succeeded", "failed", "skipped", "cancelled"}
ACTIVE_NODE_STATUSES = {"running"}
WAITING_NODE_STATUSES = {"waiting_human", "waiting_external"}
RETRY_NODE_STATUSES = {"retry_wait"}
NODE_STATUSES = (
    {"pending"}
    | ACTIVE_NODE_STATUSES
    | WAITING_NODE_STATUSES
    | RETRY_NODE_STATUSES
    | TERMINAL_NODE_STATUSES
)
RUN_STATUSES = {"pending", "running", "waiting", "succeeded", "failed", "cancelled"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ClaimedNode:
    run_id: str
    node_id: str
    attempt: int
    lease_owner: str
    lease_expires_at: str
    node: TaskNode


class RuntimeEventStore:
    """Authoritative runtime-v1 state with append-only events and derived views."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_runs (
                    run_id TEXT PRIMARY KEY,
                    graph_id TEXT NOT NULL,
                    graph_version TEXT NOT NULL,
                    planner_policy_version TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    graph_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    failure_reason TEXT
                );

                CREATE TABLE IF NOT EXISTS runtime_nodes (
                    run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    node_json TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    next_attempt_at TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    output_json TEXT,
                    error_json TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, node_id),
                    UNIQUE (run_id, idempotency_key),
                    FOREIGN KEY (run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS runtime_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    node_id TEXT,
                    event_type TEXT NOT NULL,
                    attempt INTEGER,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS runtime_artifacts (
                    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    node_id TEXT,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    sha256 TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (run_id, path),
                    FOREIGN KEY (run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_runtime_nodes_status
                    ON runtime_nodes(run_id, status, next_attempt_at);
                CREATE INDEX IF NOT EXISTS idx_runtime_events_run
                    ON runtime_events(run_id, event_id);
                """
            )

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        event_type: str,
        actor: str,
        node_id: str | None = None,
        attempt: int | None = None,
        payload: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO runtime_events
                (run_id, node_id, event_type, attempt, actor, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                node_id,
                event_type,
                attempt,
                actor,
                _canonical_json(payload or {}),
                created_at or utc_now(),
            ),
        )
        return int(cursor.lastrowid)

    def register_graph(self, graph: TaskGraph, *, actor: str = "planner") -> bool:
        graph.require_valid()
        graph_json = _canonical_json(graph.to_dict())
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT graph_json FROM runtime_runs WHERE run_id = ?", (graph.run_id,)
            ).fetchone()
            if existing:
                if str(existing["graph_json"]) != graph_json:
                    raise ValueError(
                        f"run_id {graph.run_id} is already bound to a different task graph"
                    )
                return False

            connection.execute(
                """
                INSERT INTO runtime_runs
                    (run_id, graph_id, graph_version, planner_policy_version, objective,
                     status, graph_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    graph.run_id,
                    graph.graph_id,
                    graph.version,
                    graph.planner_policy_version,
                    graph.objective,
                    graph_json,
                    now,
                    now,
                ),
            )
            for node in graph.nodes:
                connection.execute(
                    """
                    INSERT INTO runtime_nodes
                        (run_id, node_id, status, attempt, max_attempts, idempotency_key,
                         node_json, updated_at)
                    VALUES (?, ?, 'pending', 0, ?, ?, ?, ?)
                    """,
                    (
                        graph.run_id,
                        node.node_id,
                        node.retry_policy.max_attempts,
                        node.idempotency_key,
                        _canonical_json(node.to_dict()),
                        now,
                    ),
                )
            self._append_event(
                connection,
                run_id=graph.run_id,
                event_type="graph_registered",
                actor=actor,
                payload={
                    "graph_id": graph.graph_id,
                    "graph_version": graph.version,
                    "node_count": len(graph.nodes),
                },
                created_at=now,
            )
        return True

    def get_graph(self, run_id: str) -> TaskGraph:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT graph_json FROM runtime_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            raise KeyError(run_id)
        return TaskGraph.from_dict(_load_json(str(row["graph_json"]), {}))

    def node_statuses(self, run_id: str) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT node_id, status FROM runtime_nodes WHERE run_id = ?", (run_id,)
            ).fetchall()
        return {str(row["node_id"]): str(row["status"]) for row in rows}

    def _dependencies_succeeded(
        self, connection: sqlite3.Connection, run_id: str, node: TaskNode
    ) -> bool:
        if not node.depends_on:
            return True
        placeholders = ",".join("?" for _ in node.depends_on)
        rows = connection.execute(
            f"SELECT node_id, status FROM runtime_nodes WHERE run_id = ? AND node_id IN ({placeholders})",
            (run_id, *node.depends_on),
        ).fetchall()
        statuses = {str(row["node_id"]): str(row["status"]) for row in rows}
        return all(statuses.get(dependency) in {"succeeded", "skipped"} for dependency in node.depends_on)

    def claim_ready_node(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 300,
        now: str | None = None,
    ) -> ClaimedNode | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        timestamp = now or utc_now()
        lease_expires_at = (_parse_utc(timestamp) + timedelta(seconds=lease_seconds)).isoformat(
            timespec="seconds"
        )
        with self.transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT * FROM runtime_nodes
                WHERE run_id = ? AND status IN ('pending', 'retry_wait')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY rowid
                """,
                (run_id, timestamp),
            ).fetchall()
            for row in rows:
                node = TaskNode.from_dict(_load_json(str(row["node_json"]), {}))
                if not self._dependencies_succeeded(connection, run_id, node):
                    continue
                attempt = int(row["attempt"]) + 1
                updated = connection.execute(
                    """
                    UPDATE runtime_nodes
                    SET status = 'running', attempt = ?, lease_owner = ?, lease_expires_at = ?,
                        next_attempt_at = NULL, started_at = COALESCE(started_at, ?),
                        updated_at = ?, error_json = NULL
                    WHERE run_id = ? AND node_id = ? AND status IN ('pending', 'retry_wait')
                    """,
                    (
                        attempt,
                        worker_id,
                        lease_expires_at,
                        timestamp,
                        timestamp,
                        run_id,
                        node.node_id,
                    ),
                )
                if updated.rowcount != 1:
                    continue
                connection.execute(
                    "UPDATE runtime_runs SET status = 'running', updated_at = ? WHERE run_id = ?",
                    (timestamp, run_id),
                )
                self._append_event(
                    connection,
                    run_id=run_id,
                    node_id=node.node_id,
                    event_type="node_claimed",
                    attempt=attempt,
                    actor=worker_id,
                    payload={"lease_expires_at": lease_expires_at},
                    created_at=timestamp,
                )
                return ClaimedNode(
                    run_id=run_id,
                    node_id=node.node_id,
                    attempt=attempt,
                    lease_owner=worker_id,
                    lease_expires_at=lease_expires_at,
                    node=node,
                )
        return None

    def _require_claim(
        self,
        connection: sqlite3.Connection,
        claim: ClaimedNode,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM runtime_nodes WHERE run_id = ? AND node_id = ?",
            (claim.run_id, claim.node_id),
        ).fetchone()
        if not row:
            raise KeyError((claim.run_id, claim.node_id))
        if str(row["status"]) != "running":
            raise ValueError(f"node {claim.node_id} is not running")
        if str(row["lease_owner"] or "") != claim.lease_owner:
            raise ValueError(f"worker {claim.lease_owner} does not own node {claim.node_id}")
        if int(row["attempt"]) != claim.attempt:
            raise ValueError(f"stale claim for node {claim.node_id}")
        return row

    def complete_node(
        self,
        claim: ClaimedNode,
        *,
        output: dict[str, Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> None:
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            self._require_claim(connection, claim)
            connection.execute(
                """
                UPDATE runtime_nodes
                SET status = 'succeeded', output_json = ?, completed_at = ?, updated_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL, next_attempt_at = NULL
                WHERE run_id = ? AND node_id = ?
                """,
                (_canonical_json(output or {}), now, now, claim.run_id, claim.node_id),
            )
            for artifact in artifacts or []:
                connection.execute(
                    """
                    INSERT INTO runtime_artifacts
                        (run_id, node_id, kind, path, sha256, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, path) DO UPDATE SET
                        node_id = excluded.node_id,
                        kind = excluded.kind,
                        sha256 = excluded.sha256,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        claim.run_id,
                        claim.node_id,
                        str(artifact.get("kind") or "file"),
                        str(artifact["path"]),
                        artifact.get("sha256"),
                        _canonical_json(artifact.get("metadata") or {}),
                        now,
                    ),
                )
            self._append_event(
                connection,
                run_id=claim.run_id,
                node_id=claim.node_id,
                event_type="node_succeeded",
                attempt=claim.attempt,
                actor=claim.lease_owner,
                payload={"output": output or {}, "artifact_count": len(artifacts or [])},
                created_at=now,
            )
            self._refresh_run_status(connection, claim.run_id, now=now)

    def wait_node(
        self,
        claim: ClaimedNode,
        *,
        reason: str,
        waiting_status: str = "waiting_human",
        payload: dict[str, Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> None:
        if waiting_status not in {"waiting_human", "waiting_external"}:
            raise ValueError(f"unsupported waiting status: {waiting_status}")
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            self._require_claim(connection, claim)
            connection.execute(
                """
                UPDATE runtime_nodes
                SET status = ?, output_json = ?, updated_at = ?, lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE run_id = ? AND node_id = ?
                """,
                (
                    waiting_status,
                    _canonical_json({"reason": reason, **(payload or {})}),
                    now,
                    claim.run_id,
                    claim.node_id,
                ),
            )
            for artifact in artifacts or []:
                connection.execute(
                    """
                    INSERT INTO runtime_artifacts
                        (run_id, node_id, kind, path, sha256, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, path) DO UPDATE SET
                        node_id = excluded.node_id,
                        kind = excluded.kind,
                        sha256 = excluded.sha256,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        claim.run_id,
                        claim.node_id,
                        str(artifact.get("kind") or "file"),
                        str(artifact["path"]),
                        artifact.get("sha256"),
                        _canonical_json(artifact.get("metadata") or {}),
                        now,
                    ),
                )
            self._append_event(
                connection,
                run_id=claim.run_id,
                node_id=claim.node_id,
                event_type=waiting_status,
                attempt=claim.attempt,
                actor=claim.lease_owner,
                payload={
                    "reason": reason,
                    "artifact_count": len(artifacts or []),
                    **(payload or {}),
                },
                created_at=now,
            )
            self._refresh_run_status(connection, claim.run_id, now=now)

    def resolve_waiting_node(
        self,
        run_id: str,
        node_id: str,
        *,
        actor: str,
        decision: str,
        output: dict[str, Any] | None = None,
    ) -> None:
        if decision not in {"approve", "reject", "resume", "skip"}:
            raise ValueError(f"unsupported waiting-node decision: {decision}")
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT status, attempt FROM runtime_nodes WHERE run_id = ? AND node_id = ?",
                (run_id, node_id),
            ).fetchone()
            if not row or str(row["status"]) not in {"waiting_human", "waiting_external"}:
                raise ValueError(f"node {node_id} is not waiting")
            status = {
                "approve": "succeeded",
                "skip": "skipped",
                "resume": "pending",
                "reject": "failed",
            }[decision]
            completed_at = now if status in TERMINAL_NODE_STATUSES else None
            connection.execute(
                """
                UPDATE runtime_nodes
                SET status = ?, output_json = ?, error_json = ?, completed_at = ?, updated_at = ?
                WHERE run_id = ? AND node_id = ?
                """,
                (
                    status,
                    _canonical_json(output or {}) if decision != "reject" else None,
                    _canonical_json({"decision": "reject", **(output or {})})
                    if decision == "reject"
                    else None,
                    completed_at,
                    now,
                    run_id,
                    node_id,
                ),
            )
            self._append_event(
                connection,
                run_id=run_id,
                node_id=node_id,
                event_type=f"waiting_node_{decision}",
                attempt=int(row["attempt"]),
                actor=actor,
                payload=output or {},
                created_at=now,
            )
            self._refresh_run_status(connection, run_id, now=now)

    def fail_node(
        self,
        claim: ClaimedNode,
        *,
        error: dict[str, Any],
        retryable: bool = True,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> str:
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            row = self._require_claim(connection, claim)
            max_attempts = int(row["max_attempts"])
            should_retry = retryable and claim.attempt < max_attempts
            if should_retry:
                delay = max(0, claim.node.retry_policy.backoff_seconds)
                next_attempt_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=delay)
                ).isoformat(timespec="seconds")
                status = "retry_wait"
                completed_at = None
                event_type = "node_retry_scheduled"
            else:
                next_attempt_at = None
                status = "failed"
                completed_at = now
                event_type = "node_failed"
            connection.execute(
                """
                UPDATE runtime_nodes
                SET status = ?, error_json = ?, next_attempt_at = ?, completed_at = ?,
                    updated_at = ?, lease_owner = NULL, lease_expires_at = NULL
                WHERE run_id = ? AND node_id = ?
                """,
                (
                    status,
                    _canonical_json(error),
                    next_attempt_at,
                    completed_at,
                    now,
                    claim.run_id,
                    claim.node_id,
                ),
            )
            for artifact in artifacts or []:
                connection.execute(
                    """
                    INSERT INTO runtime_artifacts
                        (run_id, node_id, kind, path, sha256, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, path) DO UPDATE SET
                        node_id = excluded.node_id,
                        kind = excluded.kind,
                        sha256 = excluded.sha256,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        claim.run_id,
                        claim.node_id,
                        str(artifact.get("kind") or "file"),
                        str(artifact["path"]),
                        artifact.get("sha256"),
                        _canonical_json(artifact.get("metadata") or {}),
                        now,
                    ),
                )
            self._append_event(
                connection,
                run_id=claim.run_id,
                node_id=claim.node_id,
                event_type=event_type,
                attempt=claim.attempt,
                actor=claim.lease_owner,
                payload={
                    "error": error,
                    "next_attempt_at": next_attempt_at,
                    "artifact_count": len(artifacts or []),
                },
                created_at=now,
            )
            self._refresh_run_status(connection, claim.run_id, now=now)
        return status

    def recover_expired_leases(self, *, now: str | None = None, actor: str = "recovery") -> int:
        timestamp = now or utc_now()
        recovered = 0
        with self.transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT run_id, node_id, attempt, max_attempts, lease_owner
                FROM runtime_nodes
                WHERE status = 'running' AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                """,
                (timestamp,),
            ).fetchall()
            touched_runs: set[str] = set()
            for row in rows:
                status = "retry_wait" if int(row["attempt"]) < int(row["max_attempts"]) else "failed"
                connection.execute(
                    """
                    UPDATE runtime_nodes
                    SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                        next_attempt_at = ?, completed_at = ?, updated_at = ?, error_json = ?
                    WHERE run_id = ? AND node_id = ? AND status = 'running'
                    """,
                    (
                        status,
                        timestamp if status == "retry_wait" else None,
                        timestamp if status == "failed" else None,
                        timestamp,
                        _canonical_json({"type": "lease_expired", "previous_owner": row["lease_owner"]}),
                        row["run_id"],
                        row["node_id"],
                    ),
                )
                self._append_event(
                    connection,
                    run_id=str(row["run_id"]),
                    node_id=str(row["node_id"]),
                    event_type="node_lease_expired",
                    attempt=int(row["attempt"]),
                    actor=actor,
                    payload={"previous_owner": row["lease_owner"], "new_status": status},
                    created_at=timestamp,
                )
                touched_runs.add(str(row["run_id"]))
                recovered += 1
            for run_id in touched_runs:
                self._refresh_run_status(connection, run_id, now=timestamp)
        return recovered

    def _refresh_run_status(
        self, connection: sqlite3.Connection, run_id: str, *, now: str
    ) -> str:
        rows = connection.execute(
            "SELECT status FROM runtime_nodes WHERE run_id = ?", (run_id,)
        ).fetchall()
        statuses = [str(row["status"]) for row in rows]
        if statuses and all(status in {"succeeded", "skipped"} for status in statuses):
            status = "succeeded"
        elif any(status == "failed" for status in statuses):
            status = "failed"
        elif any(status in WAITING_NODE_STATUSES for status in statuses):
            status = "waiting"
        elif any(status in {"running", "retry_wait"} for status in statuses):
            status = "running"
        else:
            status = "pending"
        completed_at = now if status in {"succeeded", "failed", "cancelled"} else None
        connection.execute(
            """
            UPDATE runtime_runs
            SET status = ?, updated_at = ?, completed_at = ?
            WHERE run_id = ?
            """,
            (status, now, completed_at, run_id),
        )
        return status

    def run_snapshot(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            run = connection.execute(
                "SELECT * FROM runtime_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not run:
                raise KeyError(run_id)
            nodes = connection.execute(
                "SELECT * FROM runtime_nodes WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
            artifact_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM runtime_artifacts WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
            )
            event_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM runtime_events WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
            )
        return {
            "run_id": run_id,
            "graph_id": str(run["graph_id"]),
            "objective": str(run["objective"]),
            "status": str(run["status"]),
            "created_at": str(run["created_at"]),
            "updated_at": str(run["updated_at"]),
            "completed_at": run["completed_at"],
            "event_count": event_count,
            "artifact_count": artifact_count,
            "nodes": [
                {
                    "node_id": str(row["node_id"]),
                    "status": str(row["status"]),
                    "attempt": int(row["attempt"]),
                    "max_attempts": int(row["max_attempts"]),
                    "lease_owner": row["lease_owner"],
                    "lease_expires_at": row["lease_expires_at"],
                    "next_attempt_at": row["next_attempt_at"],
                    "output": _load_json(row["output_json"], {}),
                    "error": _load_json(row["error_json"], {}),
                }
                for row in nodes
            ],
        }

    def events(self, run_id: str, *, after_event_id: int = 0) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runtime_events
                WHERE run_id = ? AND event_id > ? ORDER BY event_id
                """,
                (run_id, after_event_id),
            ).fetchall()
        return [
            {
                "event_id": int(row["event_id"]),
                "run_id": str(row["run_id"]),
                "node_id": row["node_id"],
                "event_type": str(row["event_type"]),
                "attempt": row["attempt"],
                "actor": str(row["actor"]),
                "payload": _load_json(row["payload_json"], {}),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_artifacts WHERE run_id = ? ORDER BY artifact_id",
                (run_id,),
            ).fetchall()
        return [
            {
                "artifact_id": int(row["artifact_id"]),
                "run_id": str(row["run_id"]),
                "node_id": row["node_id"],
                "kind": str(row["kind"]),
                "path": str(row["path"]),
                "sha256": row["sha256"],
                "metadata": _load_json(row["metadata_json"], {}),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def export_canonical_views(self, run_id: str, output_dir: str | Path) -> list[Path]:
        target = Path(output_dir)
        target.mkdir(parents=True, exist_ok=True)
        snapshot_path = target / "runtime_snapshot.json"
        events_path = target / "runtime_events.jsonl"
        snapshot_path.write_text(
            json.dumps(self.run_snapshot(run_id), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with events_path.open("w", encoding="utf-8") as handle:
            for event in self.events(run_id):
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return [snapshot_path, events_path]
