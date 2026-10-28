# Prototype architecture

```text
Browser dashboard
  ├── bundled scenario fixtures (offline fallback)
  └── serverless API
       ├── weather/hazard provider
       ├── transparent risk rules
       └── approved LLM + vetted guidance
```

## Design constraints

- Keep secrets and provider credentials server-side.
- Preserve timestamps and attribution from every external source.
- Return the factors behind a score, not only the score itself.
- Treat generated recommendations as draft guidance requiring human review.
- Degrade safely to labeled mock data when integrations are unavailable.
