# Arion Judgment Core

> **Reality provides experience; counterfactuals produce agency.**

Arion Judgment Core is a small framework for organizing how a collective
judgment forms, remains accountable, accepts challenge, and changes when
reality contradicts it.

It is not a fixed role pipeline. A Domain Pack supplies governed roles,
question profiles, routes, and executors. Core keeps the durable judgment
state and creates temporary Agent instances only for the open questions that
currently matter.

## Core Ideas

- **Incompleteness:** unknowns remain visible instead of being erased by
  workflow completion.
- **Asymmetry:** action and inaction can carry different, irreversible costs.
- **Revisability:** every closed judgment records what would reopen it.
- **Responsibility conservation:** execution can be delegated; accountability
  cannot disappear.

## Runtime Model

- `Task` preserves the long-lived objective and accountability boundary.
- `Working Set` preserves experience, unknowns, hypotheses, counterfactuals,
  obligations, and the current judgment.
- `Run` is one bounded attempt to revise that Working Set.
- `Direct Run` handles one bounded assignment.
- `TaskGraph` appears only for multiple assignments, real dependencies,
  waiting, retries, or governed actions.
- A governance role may own multiple parallel Agent instances.

## Try the Reference Pack

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
pytest -q
```

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
print(planned.capability_plan.to_dict())
```

The included reference pack is entirely synthetic. It demonstrates the
one-way dependency `Domain Pack -> Arion Core` without business data,
production identity, or private operating knowledge.

## Boundaries

Core does not ship business vocabulary, data connectors, model pipelines,
release procedures, credentials, runtime payloads, or a default governance
organization. A Domain Pack may narrow Core safety rules but may not weaken
them.

See [Principles](docs/principles.md),
[Architecture](docs/architecture.md), and the
[Domain Pack contract](docs/contracts/arion-domain-pack.v1.yaml).

## License

Apache License 2.0.
