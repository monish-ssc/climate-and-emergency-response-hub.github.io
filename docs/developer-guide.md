# Developer guide

This guide explains how ClimateGuard AI is set up and where to add new work.

## What the project does

ClimateGuard AI is a hackathon prototype for exploring climate-related emergency risks in Canada. The dashboard lets a user choose a region and a hazard, view a simulated risk score, and generate a sample preparedness briefing.

The project is split into two main parts:

- A supplied, self-contained HTML frontend.
- A backend skeleton for future weather, risk, and AI integrations.

The current dashboard uses simulated data. It must not be used for real emergency decisions.

## Project layout

```text
.
├── index.html
├── ClimateGuard_AI_Interactive_Dashboard.html
├── api/
│   ├── contracts/
│   └── examples/
├── assets/
├── backend/
│   ├── src/
│   │   ├── providers/
│   │   ├── routes/
│   │   └── services/
│   └── tests/
│       ├── contract/
│       └── unit/
├── data/
├── docs/
└── tests/
```

### Frontend

`ClimateGuard_AI_Interactive_Dashboard.html` is the main frontend. It contains its own HTML, CSS, JavaScript, map image, and mock scenario data. Keep it as the single source of truth for the user interface.

`index.html` is a small GitHub Pages entry point. It sends the browser to the main dashboard.

Avoid creating a second frontend or splitting the supplied HTML into separate files during the hackathon. If the frontend needs live data, add a small API adapter inside the existing script and leave the rest of the page intact.

### API contracts

The `api/contracts/` folder contains JSON Schemas for the planned endpoints:

- `POST /api/risk` returns a normalized and explainable risk assessment.
- `POST /api/briefing` returns a preparedness briefing that requires human review.

The `api/examples/` folder contains complete example requests and responses. Use these examples when building the backend or connecting the frontend.

Read `api/README.md` before changing a request or response. Contract changes should be agreed on before frontend and backend code are updated.

### Backend

The backend folders are empty by design so the team can choose a runtime or serverless platform.

- `backend/src/providers/` is for code that calls weather, hazard, or AI providers.
- `backend/src/services/` is for data normalization, risk rules, and briefing logic.
- `backend/src/routes/` is for the two HTTP endpoint handlers.
- `backend/tests/unit/` is for small tests of risk and normalization logic.
- `backend/tests/contract/` is for checking responses against the JSON Schemas.

Keep provider-specific code inside `providers`. The rest of the backend should work with the normalized fields defined by the contracts.

### Demo data

`data/scenarios.json` is the offline fallback fixture. It lets the demo continue if a live provider is unavailable. Any screen using this file must clearly say that the data is simulated.

## Run the frontend

The frontend does not need a build step or package install.

From the repository root, run:

```bash
python3 -m http.server 8000
```

Then open:

```text
http://localhost:8000/
```

You can also open `ClimateGuard_AI_Interactive_Dashboard.html` directly, but a local server is better when testing API requests.

## Connect the frontend to the API

The HTML and API use slightly different hazard names. Translate them in one small adapter function:

| Dashboard value | API value |
| --- | --- |
| `wildfire` | `wildfire` |
| `rain` | `flood` |
| `hurricane` | `severe_storm` |

A simple request flow should look like this:

1. The user selects a region and hazard in the dashboard.
2. The adapter translates the hazard name.
3. The browser sends a request that matches `risk-request.schema.json`.
4. The backend normalizes provider data and calculates the risk score.
5. The browser renders the response or falls back to the existing mock data.
6. The briefing button sends the normalized assessment to `/api/briefing`.

Do not send provider credentials or LLM keys to the browser. Store secrets in the backend platform's environment settings.

## Suggested order of work

1. Choose a backend runtime and add its setup files under `backend/`.
2. Implement `/api/risk` with `data/scenarios.json` first.
3. Add contract tests for the risk response.
4. Connect one live weather provider through `backend/src/providers/`.
5. Add the small frontend API adapter with a safe mock-data fallback.
6. Implement `/api/briefing` using approved guidance.
7. Test the full demo with the network available and unavailable.

Starting with the local fixture makes it easier to test the full request flow before depending on external services.

## Testing changes

For frontend changes, work through `tests/manual-checklist.md`. Check the desktop and mobile layouts, keyboard controls, hazard tabs, region selection, and briefing dialog.

For backend changes, add tests that cover:

- Valid and invalid request bodies.
- Risk scores at the minimum and maximum boundaries.
- Provider errors and timeouts.
- Cached or simulated fallback data.
- Response validation against the JSON Schemas.
- Briefings with citations and `requiresHumanReview` set to `true`.

## Project rules

- Keep the supplied HTML as the only frontend.
- Never commit secrets, API keys, or private credentials.
- Always include units, timestamps, and source information in live data.
- Keep the risk calculation simple and explainable.
- Clearly label simulated, cached, and live data.
- Treat AI briefings as drafts that require review by an authorized person.
- Keep the demo working when an external service fails.

## Before opening a pull request

- Run the frontend locally and complete the manual checklist.
- Validate any changed JSON files.
- Check that API examples still match their schemas.
- Make sure no credentials or local environment files are included.
- Update this guide and `api/README.md` if the setup or contracts changed.
