# Arion Judgment Core 使用指南

> 从第一个有界问题，到自己的 Domain Pack。

这份指南不重复完整架构定义，而是回答三个实际问题：

1. 什么情况适合使用 Arion；
2. 什么时候使用 Direct Run，什么时候使用 TaskGraph；
3. 怎样在不把系统做重的前提下，建立自己的角色、路由与执行器。

## 1. 先理解 Arion 的工作对象

Arion 的工作对象不是“任务列表”，而是一个仍在变化的**集体判断**。

一个判断至少包含：

- 当前观察到了什么；
- 哪些内容已经知道，哪些仍然不知道；
- 哪些解释只是暂时假设；
- 还有哪些竞争假设和反事实；
- 如果判断错误，行动与不行动分别有什么代价；
- 当前愿意承担什么结论；
- 什么新经验会使这个结论重新打开；
- 哪些未完成事项必须继续观察。

因此，一次执行成功不等于一个 Task 已经关闭。执行只是为 Working Set 增加经验，或修正其中的已知、未知、判断与义务。

## 2. 固定什么，动态什么

推荐把 Arion 分成五层：

| 层 | 生命周期 | 负责什么 |
| --- | --- | --- |
| 治理角色 | 稳定 | 责任、权限、不可约问题和停止边界 |
| Domain Pack | 版本化 | 角色、问题契约、路由、安全边界和执行器绑定 |
| Agent 实例 | 临时 | 回答一个有界问题，不继承额外权力 |
| Run | 临时 | 完成一次有界执行并留下证据 |
| Task + Working Set | 持续 | 保存目标、责任和可被现实修正的当前判断 |

最重要的区分是：

> **角色是责任容器，Agent 是问题执行实例。**

不要因为想并行，就复制更多角色；也不要因为角色固定，就让所有角色每次都出场。一个角色面对两个相互独立的未知项时，可以生成两个并行 Agent，而责任仍然归属于同一个角色。

## 3. 推荐的五种用法

### 3.1 一个有界、只读的问题

例如：

- 检查一份本地证据是否满足约定；
- 对一个已知输入做确定性分析；
- 让一个专业角色回答一个明确问题。

推荐使用 `Direct Run`。

适用条件：

- 只有一个活动治理角色；
- 只有一个任务契约；
- 不需要等待人工或外部系统；
- 不存在真实依赖；
- 副作用为 `none` 或 `local_read`。

不要为了“看起来像多 Agent”而创建图。

### 3.2 多个相互独立的未知项

例如同一个 Investigator 同时需要检验：

- 当前观察边界是否可靠；
- 另一种解释是否仍然成立。

推荐让 planner 从 Working Set 中创建两个并行 Agent 实例。它们可以属于同一个治理角色，也不需要增加新的永久角色。

### 3.3 存在真实依赖

只有当后一个问题确实依赖前一个结果时，才加入 `depends_on` 并使用 `TaskGraph`。

不要把组织习惯写成依赖。例如“分析后必须轮到策略”只是惯例；只有“策略问题无法在缺少该分析结果时成立”才是真依赖。

### 3.4 需要等待或人工判断

以下情况适合 `TaskGraph`：

- 等待人工确认；
- 等待外部证据；
- 需要可恢复的重试；
- 需要独立 assurance；
- 需要最终责任人决定关闭、继续或重定向。

等待是显式状态，不应被伪装成成功或失败。恢复执行时应继续同一个 Run，而不是重建一条无法追溯的新流程。

### 3.5 高风险行动前的治理

公开 Core 可以组织：

- 候选方案；
- 证据要求；
- assurance；
- 人工 gate；
- 关闭与修正条件。

但公开 Core 的节点禁止 `remote_write` 和 `production_write`。真实远端或生产行动应留在独立、受治理的行动系统中，并通过 Domain Pack 记录责任与证据契约，而不是削弱 Core 的安全边界。

## 4. 不推荐的用法

### 不要把角色写成固定流水线

错误方向：

```text
Role A -> Role B -> Role C -> Role D
```

推荐方向：

```text
当前开放问题
-> 找到问题所属的治理角色
-> 为每个不可约问题创建 Agent
-> 只添加真实依赖
```

### 不要每次激活所有角色

角色注册表定义“谁可以负责”，不是“谁必须参加”。没有开放问题的角色不应仅为了形式完整而运行。

### 不要把对话记录当作 Working Set

对话可以是证据来源，但不是长期真相。真正需要保留的是结构化的经验、未知、假设、当前判断、义务和修正触发器。

### 不要用 DAG 掩盖不清楚的问题

先定义问题与责任，再生成执行结构。不要先画一张复杂图，再试图给每个节点寻找存在理由。

### 不要用“执行完成”抹掉未知项

关闭判断时，仍然开放的未知和反事实必须：

- 已解决；
- 明确接受，并绑定后续观察义务；
- 或继续保持开放。

它们不能因为报告已经生成而消失。

### 不要把运行产物放进源码仓库

推荐把 `ARION_HOME` 放在源码仓库之外：

```bash
export ARION_HOME="$HOME/.local/share/arion"
```

Core 默认也会使用这个外置位置。任务状态、Run、数据集、模型、缓存和其他运行产物不应进入 Git 历史，也不应与源码目录的云盘同步策略绑定。

## 5. 第一次运行

### 5.1 安装并验证

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
pytest -q
```

### 5.2 加载 Reference Pack

```python
from pathlib import Path

from planner.domain_pack import load_domain_pack

pack = load_domain_pack(
    pack_root=Path("open_source/reference_pack"),
    manifest_path="manifest.yaml",
)
```

### 5.3 创建长期 Task

```python
from planner.runtime_paths import build_runtime_paths
from planner.working_set_store import WorkingSetStore

paths = build_runtime_paths(create=True)
working_sets = WorkingSetStore(paths.tasks)

task, working_set = working_sets.create_task(
    task_id="first-inquiry",
    objective="Inspect one bounded synthetic observation.",
    user_intent="Learn the smallest Arion execution path.",
    acceptance_boundary="Use synthetic local evidence only.",
    accountable_owner=pack.governance.orchestrator_role,
)
```

`Task` 应持续存在。以后每次获得新经验，都创建新的 Run，并修正这个 Task 的 Working Set。

### 5.4 先生成计划，不要立刻执行

```python
from planner.semantic_execution_planner import plan_runtime

planned = plan_runtime(
    domain_pack=pack,
    question=task.objective,
    run_id="first-inquiry-run-001",
    working_set=working_set.to_dict(),
)

execution_plan = planned.direct_run or planned.task_graph
issues = execution_plan.validate()
if issues:
    raise ValueError(issues)

print(planned.orchestration_mode)
print(planned.decision.to_dict())
print(execution_plan.to_dict())
```

执行前至少检查：

- `selected_route` 是否回答了原问题；
- `active_roles` 是否只有真正需要承担问题的角色；
- `agent_assignments` 是否对应当前开放问题；
- `orchestration_mode` 是否过重；
- `boundaries` 是否保持本地与只读约束；
- `validate()` 是否返回空列表。

### 5.5 执行 Direct Run

Reference Pack 的普通有界观察会生成 Direct Run：

```python
from planner.direct_execution import (
    DirectRunEngine,
    build_direct_executor_registry,
)

if planned.direct_run is None:
    raise ValueError("This example expects a Direct Run.")

engine = DirectRunEngine(
    registry=build_direct_executor_registry(domain_pack=pack),
    run_root=paths.runs,
    working_sets=working_sets,
)
result = engine.run(planned.direct_run, task_id=task.task_id)

print(result["status"])
print(result["output"])
```

执行器的输出会留下 Run 证据，并以新版本修正 Working Set。

## 6. 什么时候升级为 TaskGraph

使用包含 `counterfactual`、`alternative explanation` 或 `competing hypothesis` 的 Reference Pack 问题，会生成 TaskGraph：

```python
task, working_set = working_sets.load_context(task.task_id)

planned = plan_runtime(
    domain_pack=pack,
    question="Explore a counterfactual and competing hypothesis.",
    run_id="counterfactual-run-001",
    working_set=working_set.to_dict(),
)

assert planned.task_graph is not None
```

推荐通过 `TaskGraphExecutionCoordinator` 执行，因为它会把图的运行结果同步回 Working Set：

```python
from planner.runtime_event_store import RuntimeEventStore
from planner.task_graph_execution import TaskGraphExecutionCoordinator
from planner.unified_execution import (
    UnifiedExecutionEngine,
    build_task_graph_executor_registry,
)

coordinator = TaskGraphExecutionCoordinator(
    engine=UnifiedExecutionEngine(
        store=RuntimeEventStore(paths.runtime_state_db),
        registry=build_task_graph_executor_registry(domain_pack=pack),
        run_root=paths.runs,
    ),
    working_sets=working_sets,
)

snapshot = coordinator.run(
    planned.task_graph,
    task_id=task.task_id,
)
print(snapshot["status"])
```

如果图停在人工 gate，应先阅读节点输出和当前 Working Set，再调用 `coordinator.resolve(...)`。关闭输出必须如实说明开放未知项和反事实的处置方式，并记录 revision triggers；完整可执行示例见 [`open_source/test_core_judgment.py`](../open_source/test_core_judgment.py)。

## 7. 建立自己的 Domain Pack

最安全的起点是复制 Reference Pack，然后逐项替换：

```bash
cp -R open_source/reference_pack my_domain_pack
```

每个文件只承担一种责任：

| 文件 | 推荐职责 |
| --- | --- |
| `manifest.yaml` | Pack 身份、版本和其他契约文件的位置 |
| `roles.yaml` | 稳定治理角色、orchestrator 与角色输出位置 |
| `role_contracts.yaml` | 每个角色的不可约问题、边界、输出与停止条件 |
| `runtime_policy.yaml` | 已知问题模式、活动角色和编排方式 |
| `route_contracts.yaml` | 问题意图、路由、前置条件与候选下一步 |
| `executor_bindings.yaml` | 计划中的 executor id 如何绑定到显式 callable |
| `executors.py` | 领域执行器实现 |

### 7.1 先定义最少角色

角色应对应无法被其他角色约掉的责任，而不是现有组织架构里的每个部门。

为每个角色回答：

1. 它必须独立回答的不可约问题是什么？
2. 它可以决定什么，不能决定什么？
3. 什么输出才算完成？
4. 应在什么边界停止并移交？

如果两个角色没有不同的责任或判断权，优先合并。

### 7.2 再定义问题路由

路由应描述问题家族，而不是流程阶段。

好的路由：

- bounded evidence inquiry；
- competing hypothesis review；
- independent assurance。

不好的路由：

- step 1；
- analyst stage；
- manager review stage。

只有稳定、反复出现且边界清楚的场景，才值得在 `runtime_policy.yaml` 中增加显式 workflow binding。其他问题应由默认 route 和 Working Set 动态决定。

### 7.3 最后绑定执行器

执行器绑定必须显式、可审计，并与 Domain Pack 身份一致。推荐从纯函数、本地读取和合成数据开始。

执行器应返回：

- 明确的 `status`；
- 结构化 `output`；
- 可校验的 artifacts；
- 对 Working Set 的有界修正。

不要让执行器通过隐式全局状态获得额外权限。

### 7.4 语义只保留到“会改变判断”为止

不需要先建立庞大的统一语义层。只保留会实际改变以下内容的定义：

- 问题如何路由；
- 证据如何解释；
- 哪个角色负责；
- 什么边界必须遵守；
- 什么条件会修正判断。

不会改变这些内容的词汇表、历史镜像和运行输出，不应进入 Core。

## 8. 推荐的渐进式采用路径

### 阶段一：只规划

- 建立最小 Domain Pack；
- 对真实问题生成计划但不执行；
- 人工检查路由、角色、Agent 数量和边界；
- 用代表性案例修正契约。

### 阶段二：只读 Direct Run

- 选择一个低风险、有界问题；
- 使用本地或合成证据；
- 确认 Run 能正确修正 Working Set；
- 不引入 DAG。

### 阶段三：持续 Working Set

- 让同一个 Task 经历多次 Run；
- 记录未知、反事实、义务和 revision triggers；
- 验证新经验能够重新打开旧判断。

### 阶段四：动态多 Agent

- 只为多个真实开放问题创建并行 Agent；
- 只添加真实依赖；
- 增加等待、assurance 和人工 closure gate；
- 验证开放项不会在关闭时被隐藏。

## 9. 设计检查清单

在新增角色、路由或执行图之前，逐项确认：

### 判断

- 当前事实、解释和建议是否分开？
- 未知与冲突是否仍然可见？
- 是否至少存在一个可检验的反事实？
- 是否记录了判断被推翻或重开的条件？

### 责任

- 每个开放问题是否有明确治理角色？
- Agent 是否只获得该问题所需的最小权限？
- 并行执行后，最终责任是否仍然唯一可识别？

### 编排

- 一个 Direct Run 是否已经足够？
- 每条依赖是否都是真实信息依赖？
- 是否存在仅为了“角色齐全”而运行的节点？
- 等待、重试和人工决定是否被显式表示？

### 状态

- Task 与 Working Set 是否比 Run 活得更久？
- Run 是否只追加经验和修正状态，而不是成为新的真相来源？
- 关闭时是否如实处置开放未知项与反事实？

### 运行

- `ARION_HOME` 是否位于源码仓库之外？
- 运行数据、模型和 artifacts 是否被排除在 Git 之外？
- 执行器绑定是否显式并与 Domain Pack 身份一致？
- 是否保持远端写入和生产写入禁用？

## 10. 可执行参考

- [`open_source/reference_pack`](../open_source/reference_pack)：最小纯合成 Domain Pack。
- [`open_source/test_reference_pack.py`](../open_source/test_reference_pack.py)：Direct Run、TaskGraph 和私有模块隔离测试。
- [`open_source/test_core_judgment.py`](../open_source/test_core_judgment.py)：同角色多 Agent 与关闭契约测试。
- [`docs/architecture.md`](architecture.md)：核心状态与规划结构。
- [`docs/principles.md`](principles.md)：设计哲学。
- [`docs/contracts/arion-domain-pack.v1.yaml`](contracts/arion-domain-pack.v1.yaml)：Domain Pack 公开契约。

最小原则始终是：

> **先保留责任和判断，再增加执行结构；先让一个问题跑通，再让系统变复杂。**
