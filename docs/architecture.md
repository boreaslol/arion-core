# Architecture

## Dependency Direction

```text
Domain Pack -> Arion Core
```

Core never imports a concrete Domain Pack implementation. A selected Pack
provides relative paths to its role registry, runtime policy, role contracts,
route contracts, and executor bindings.

## Durable State

### Task

A Task preserves:

- objective and user intent;
- acceptance boundary;
- accountable owner;
- current Working Set version;
- active, waiting, or closed status.

### Working Set

The versioned Working Set is the operational truth for the current judgment:

```text
observed_experience
knowns
unknowns
assumptions
competing_hypotheses
counterfactuals
asymmetric_consequences
current_judgment
active_obligations
revision_triggers
artifact_refs
knowledge_refs
next_candidate_moves
```

### Run

A Run is one bounded execution. Runtime events and artifacts belong to the
Run; they revise the Working Set but do not replace it.

## Planning

Static Pack contracts provide valid roles, capabilities, and safety bounds.
The current Working Set provides the live problem state.

The planner:

1. selects the highest-priority class of open items;
2. creates one assignment per item;
3. keeps governance roles fixed;
4. creates multiple Agent instances when one role owns multiple items;
5. adds dependencies only when they are explicit;
6. uses Direct Run for exactly one bounded assignment;
7. uses TaskGraph for parallelism, dependencies, waiting, retries, or
   governed actions.

## Closure

Successful nodes do not close a Task. A closure gate must validate the
collective judgment contract. Open items can be accepted as incomplete only
when their future observation obligations are recorded.

This separates:

- execution success;
- evidence quality;
- current judgment;
- future reality that may revise the judgment.

## Safety

Core defaults to local execution and forbids production writes. Executor
registries are explicit and must match the Domain Pack identity recorded in
the execution plan.

A Domain Pack may add stronger action protocols and independent verification.
It may not weaken Core boundaries or redefine Task and Working Set state.

## Public Boundary

The public Core contains only the judgment kernel, adaptive execution
contracts, synthetic reference Pack, and tests. Domain-specific data,
models, infrastructure, knowledge, and action procedures remain outside.
