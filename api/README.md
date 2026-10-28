# API boundary

This folder documents the server-side boundary planned for the hackathon. GitHub Pages cannot run server code, so deploy these endpoints through a serverless provider if live integrations are added.

## Proposed endpoints

### `GET /api/risk?region={id}&hazard={type}`

Returns normalized measurements, an explainable risk score, source attribution, and freshness metadata.

### `POST /api/briefing`

Accepts a normalized risk assessment and returns a grounded preparedness summary. The server must validate input, protect credentials, and restrict references to approved guidance.

Never expose weather-service or LLM credentials in the frontend.
