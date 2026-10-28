# Backend integration skeleton

The backend is intentionally separated from the provided, self-contained HTML frontend. Its job is to implement the schemas in `../api/contracts/` and keep provider credentials out of the browser.

## Responsibilities

- Normalize weather and hazard-provider responses.
- Calculate an explainable prototype risk score.
- Serve `POST /api/risk` using the risk contracts.
- Serve `POST /api/briefing` using the briefing contracts.
- Fall back to `../data/scenarios.json` when demo mode is enabled.
- Add timestamps, source attribution, request IDs, and safe error responses.

## Suggested layout

```text
backend/
├── src/
│   ├── providers/      # External weather and hazard adapters
│   ├── services/       # Risk and briefing business logic
│   └── routes/         # HTTP endpoint handlers
└── tests/
    ├── contract/       # Schema validation tests
    └── unit/           # Risk and normalization tests
```

Choose the serverless/runtime platform during the hackathon, then add its dependency and deployment files here. The frontend should depend only on the HTTP contracts, not on the chosen backend framework.

The frontend adapter maps `wildfire` to `wildfire`, `rain` to `flood`, and `hurricane` to `severe_storm` before sending a request. Backend code accepts only the API values defined by the JSON Schemas.
