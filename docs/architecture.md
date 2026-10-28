# Prototype architecture

```text
Provided self-contained HTML dashboard
  ├── embedded UI and current mock behavior
  └── thin HTTP adapter (only frontend change required)
       └── serverless API
       ├── weather/hazard provider
       ├── transparent risk rules
       ├── approved LLM + vetted guidance
       └── bundled scenario fixtures (offline fallback)
```

## Design constraints

- Keep secrets and provider credentials server-side.
- Preserve timestamps and attribution from every external source.
- Return the factors behind a score, not only the score itself.
- Treat generated recommendations as draft guidance requiring human review.
- Degrade safely to labeled mock data when integrations are unavailable.
- Keep the supplied HTML as the canonical frontend; do not introduce a second UI implementation.
