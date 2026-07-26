# Arion Judgment Core

[English](README.md) | 简体中文

> **现实提供经验，反事实产生主体性。**

Arion Judgment Core 是一个小型、可修正的集体判断内核。它组织的不是一条固定分工流水线，而是一个判断如何形成、如何承担责任、如何接受质疑，以及如何在现实反驳后继续变化的过程。

它采用：

> **固定治理角色 + 动态 Agent 实例 + 可版本化 Working Set**

Domain Pack 提供稳定的责任边界、问题契约、路由和执行器；Core 保存持续存在的判断状态，并且只为当前真正开放的问题创建临时 Agent。

## 它解决什么问题

很多 Agent 系统擅长分配任务，却不擅长保存判断：

- 流程结束后，未知项随对话一起消失；
- 每次运行都机械激活同一组角色；
- 执行完成被误认为问题已经解决；
- 结论没有记录什么新事实会使它失效；
- 工作可以被委派，责任却在多 Agent 之间被稀释。

Arion 把这些问题收束为一个持续循环：

```text
现实
-> 经验
-> 反事实与竞争假设
-> 有界探究或行动
-> 新的现实
-> 被修正的判断
```

## 核心原则

- **不完备性：** 未知、冲突与证据缺口不会因为流程完成而被抹掉。
- **非对称性：** 错误行动与错误不行动的代价可能完全不同。
- **可修正性：** 每个关闭的判断都要记录重新打开它的条件。
- **责任守恒：** 执行可以委派，责任不能消失。
- **问题驱动：** 系统先问“还有哪些不可约的问题”，而不是“下一个该轮到哪个角色”。

## 运行时模型

- `Task`：保存长期目标、验收边界与责任归属。
- `Working Set`：保存经验、已知、未知、假设、反事实、义务、修正触发器与当前判断。
- `Run`：一次有界尝试，只负责修正 Working Set，不取代它。
- `Direct Run`：处理一个角色拥有的一个有界任务。
- `TaskGraph`：只在存在多个任务、真实依赖、等待、重试或治理关口时出现。
- `Agent instance`：临时执行者；同一个治理角色可以同时拥有多个 Agent 实例。

```text
                +------------------+
                |   Domain Pack    |
                | 责任、问题、边界 |
                +---------+--------+
                          |
Task + Working Set -------+-------> Planner
                                     |
                         +-----------+-----------+
                         |                       |
                    Direct Run              TaskGraph
                         |                       |
                         +-----------+-----------+
                                     |
                              修正 Working Set
```

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
pytest -q
```

使用内置的纯合成 Reference Pack 生成第一个计划：

```python
from pathlib import Path

from planner.domain_pack import load_domain_pack
from planner.semantic_execution_planner import plan_runtime

pack = load_domain_pack(
    pack_root=Path("open_source/reference_pack"),
    manifest_path="manifest.yaml",
)
planned = plan_runtime(
    domain_pack=pack,
    question="Explore a counterfactual and competing hypothesis.",
    run_id="example-run",
)

print(planned.orchestration_mode)
print(planned.decision.to_dict())
print((planned.direct_run or planned.task_graph).to_dict())
```

Reference Pack 不包含业务术语、客户数据、生产身份、私有知识或基础设施配置。它只用于演示单向依赖：

```text
Domain Pack -> Arion Core
```

## 推荐阅读顺序

1. [中文使用指南](docs/guidebook.zh-CN.md)：如何选择运行方式、执行第一个任务，以及构建自己的 Domain Pack。
2. [设计原则](docs/principles.md)：为什么系统要保留不完备性、反事实与修正条件。
3. [架构说明](docs/architecture.md)：Task、Working Set、Run 与动态 Agent 如何协作。
4. [Domain Pack 契约](docs/contracts/arion-domain-pack.v1.yaml)：公开扩展边界。

## 边界

Arion Core 不是：

- 一个固定角色流水线；
- 一个通用 LLM 提供商封装；
- 一个让所有角色参与每次运行的会议系统；
- 一个把对话记录当作长期事实的聊天框架；
- 一个可直接执行远端写入或生产变更的发布工具。

公开 Core 默认保持本地执行，并禁止远端写入与生产写入。业务语义、数据连接器、模型、基础设施、凭证、运行产物和具体行动协议都应留在独立的 Domain Pack 或受治理的外部系统中。

## License

Apache License 2.0。
