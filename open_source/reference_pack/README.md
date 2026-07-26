# Synthetic Reference Pack

This Pack contains no business vocabulary, production identity, customer data,
or private operating knowledge. It demonstrates the one-way dependency:

```text
Synthetic Domain Pack -> Arion Core
```

The baseline question uses Direct Run. A question containing
`counterfactual`, `alternative explanation`, or `competing hypothesis`
selects a TaskGraph with one `Investigator` Agent, deterministic assurance,
and a final `Coordinator` human gate.

When the Working Set contains multiple highest-priority open items owned by
`Investigator`, Core creates one parallel Agent instance per item without
changing the governance role registry.
