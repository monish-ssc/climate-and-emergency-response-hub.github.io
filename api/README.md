# API boundary

This folder documents the server-side boundary planned for the hackathon. GitHub Pages cannot run server code, so deploy these endpoints through a serverless provider if live integrations are added.

## Contract rules

- JSON request and response bodies use UTF-8 and `Content-Type: application/json`.
- Contract version `1.0` uses camelCase fields and ISO 8601 UTC timestamps.
- Numeric measurements always include an explicit unit.
- Risk scores are integers from 0 through 100 and must include contributing factors.
- Successful responses include a request ID, provenance, data mode, and generation time.
- Errors use the shared error shape in `contracts/common.schema.json`.
- Additive fields require a minor contract version; removed or renamed fields require a major version.

Schemas live in `contracts/`; complete payloads live in `examples/`.

## Proposed endpoints

### `POST /api/risk`

Accepts `risk-request.schema.json` and returns `risk-response.schema.json`.

Status codes: `200` success, `400` invalid request, `404` unknown region, `502` provider failure, `503` no acceptable live or cached data.

### `POST /api/briefing`

Accepts a normalized risk assessment and returns a grounded preparedness summary. The server must validate input, protect credentials, and restrict references to approved guidance.

Accepts `briefing-request.schema.json` and returns `briefing-response.schema.json`.

Status codes: `200` success, `400` invalid request, `422` unsupported assessment, `502` approved AI provider failure.

Never expose weather-service or LLM credentials in the frontend.
