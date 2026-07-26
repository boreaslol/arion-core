from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from planner.direct_execution import (
    DirectRunEngine,
    build_direct_executor_registry,
)
from planner.domain_pack import load_domain_pack
from planner.runtime_event_store import RuntimeEventStore
from planner.semantic_execution_planner import plan_runtime
from planner.unified_execution import (
    UnifiedExecutionEngine,
    build_task_graph_executor_registry,
)
from planner.working_set_store import WorkingSetStore


ROOT = Path(__file__).resolve().parents[1]
REFERENCE_PACK_ROOT = ROOT / "open_source" / "reference_pack"


def _reference_pack():
    return load_domain_pack(
        pack_root=REFERENCE_PACK_ROOT,
        manifest_path="manifest.yaml",
    )


def test_reference_pack_executes_direct_run(tmp_path: Path) -> None:
    domain_pack = _reference_pack()
    planned = plan_runtime(
        domain_pack=domain_pack,
        question="Inspect one bounded synthetic observation.",
        run_id="reference-direct",
    )
    assert planned.direct_run is not None
    working_sets = WorkingSetStore(tmp_path / "tasks")
    task, _ = working_sets.create_task(
        task_id="reference-direct-task",
        objective=planned.capability_plan.question,
        user_intent=planned.capability_plan.question,
        acceptance_boundary="Keep synthetic evidence bounded.",
        accountable_owner=domain_pack.governance.orchestrator_role,
    )
    engine = DirectRunEngine(
        registry=build_direct_executor_registry(domain_pack=domain_pack),
        run_root=tmp_path / "runs",
        working_sets=working_sets,
    )

    result = engine.run(planned.direct_run, task_id=task.task_id)

    assert result["status"] == "succeeded"
    assert result["output"]["status"] == "synthetic_observation_completed"
    assert (
        tmp_path
        / "runs"
        / "reference-direct"
        / "artifacts"
        / "synthetic_observation.json"
    ).is_file()


def test_reference_pack_executes_task_graph_to_orchestrator_gate(
    tmp_path: Path,
) -> None:
    domain_pack = _reference_pack()
    planned = plan_runtime(
        domain_pack=domain_pack,
        question="Explore a counterfactual and competing hypothesis.",
        run_id="reference-graph",
    )
    assert planned.task_graph is not None
    store = RuntimeEventStore(tmp_path / "runtime.sqlite3")
    engine = UnifiedExecutionEngine(
        store=store,
        registry=build_task_graph_executor_registry(
            domain_pack=domain_pack
        ),
        run_root=tmp_path / "runs",
    )

    waiting = engine.run(planned.task_graph)

    nodes = {node["node_id"]: node for node in waiting["nodes"]}
    assert waiting["status"] == "waiting"
    assert nodes["role.01.investigator"]["status"] == "succeeded"
    assert nodes["assurance.system"]["output"]["status"] == "pass"
    assert (
        nodes["gate.orchestrator-closure"]["status"] == "waiting_human"
    )
    store.resolve_waiting_node(
        planned.task_graph.run_id,
        "gate.orchestrator-closure",
        actor="Coordinator",
        decision="approve",
        output={"decision": "CLOSED", "note": "Synthetic inquiry complete."},
    )

    final = engine.resume(planned.task_graph.run_id)

    assert final["status"] == "succeeded"


def test_reference_pack_runs_with_private_modules_blocked(
    tmp_path: Path,
) -> None:
    script = f"""
import importlib.abc
from pathlib import Path
import tempfile

BLOCKED = (
    "domain_pack",
    "risk_core",
    "loan_metrics_analysis",
    "loan_metrics_queries",
    "scripts",
)

class PrivateModuleBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == prefix or fullname.startswith(prefix + ".") for prefix in BLOCKED):
            raise ImportError("private module blocked: " + fullname)
        return None

import sys
sys.meta_path.insert(0, PrivateModuleBlocker())

from planner.direct_execution import DirectRunEngine, build_direct_executor_registry
from planner.domain_pack import load_domain_pack
from planner.runtime_event_store import RuntimeEventStore
from planner.semantic_execution_planner import plan_runtime
from planner.unified_execution import UnifiedExecutionEngine, build_task_graph_executor_registry
from planner.working_set_store import WorkingSetStore

pack_root = Path({str(REFERENCE_PACK_ROOT)!r})
pack = load_domain_pack(pack_root=pack_root, manifest_path="manifest.yaml")
with tempfile.TemporaryDirectory() as raw_temp:
    temp = Path(raw_temp)
    direct = plan_runtime(
        domain_pack=pack,
        question="Inspect one bounded synthetic observation.",
        run_id="blocked-direct",
    )
    working_sets = WorkingSetStore(temp / "tasks")
    task, _ = working_sets.create_task(
        task_id="blocked-task",
        objective="Inspect synthetic evidence.",
        user_intent="Inspect synthetic evidence.",
        acceptance_boundary="Synthetic evidence only.",
        accountable_owner=pack.governance.orchestrator_role,
    )
    DirectRunEngine(
        registry=build_direct_executor_registry(domain_pack=pack),
        run_root=temp / "runs",
        working_sets=working_sets,
    ).run(direct.direct_run, task_id=task.task_id)

    graph = plan_runtime(
        domain_pack=pack,
        question="Explore a counterfactual and competing hypothesis.",
        run_id="blocked-graph",
    )
    store = RuntimeEventStore(temp / "runtime.sqlite3")
    engine = UnifiedExecutionEngine(
        store=store,
        registry=build_task_graph_executor_registry(domain_pack=pack),
        run_root=temp / "runs",
    )
    waiting = engine.run(graph.task_graph)
    assert waiting["status"] == "waiting"
    store.resolve_waiting_node(
        graph.task_graph.run_id,
        "gate.orchestrator-closure",
        actor="Coordinator",
        decision="approve",
        output={{"decision": "CLOSED"}},
    )
    assert engine.resume(graph.task_graph.run_id)["status"] == "succeeded"
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
