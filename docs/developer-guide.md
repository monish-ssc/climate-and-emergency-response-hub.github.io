# Developer guide

This guide explains how ClimateGuard AI is set up and where to add new work.

## What the project does

ClimateGuard AI is a hackathon prototype for exploring climate-related emergency risks in Canada. The dashboard lets a user choose a region and hazard, view a pipeline risk snapshot when available, and generate a sample preparedness briefing.

The project is split into two main parts:

- A supplied, self-contained HTML frontend.
- A backend skeleton for future weather, risk, and AI integrations.

The dashboard combines provisional public-data snapshots with simulated fallback scenarios. It must not be used for real emergency decisions.

## Project layout

```text
.
├── index.html
├── ClimateGuard_Mockup.html
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

`ClimateGuard_Mockup.html` is the main frontend. It contains its own HTML, CSS, JavaScript, maps, a static JSON adapter, and mock fallback scenarios. Keep it as the single source of truth for the user interface.

`index.html` is a small GitHub Pages entry point. It sends the browser to the main dashboard.

Avoid creating a second frontend or splitting the supplied HTML into separate files during the hackathon. Keep future data adapters inside the existing script and preserve the demonstration fallback.

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

The scripts in `data/` produce the static snapshots consumed by the dashboard. The weather scripts fetch live, provisional data from ECCC, while the healthcare pipeline downloads a public facility inventory from Open Government Canada. Downloads require internet access. Run all commands below from the repository root.

Create a virtual environment once (skip creation if `.venv/` already exists), then install the declared dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Use the virtual environment's interpreter directly; activation is not needed.
In VS Code, select `.venv/bin/python` with **Python: Select Interpreter** so
editor analysis uses the same dependencies as the terminal.
On Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`.

Run the scripts as needed:

```bash
.venv/bin/python data/explore_water_weather.py
.venv/bin/python data/flood_pipeline.py
.venv/bin/python data/hurricane_pipeline.py
.venv/bin/python data/healthcare_data_pipeline.py
```

### Automated live-data refresh

`.github/workflows/refresh-live-data.yml` runs every 30 minutes (at minutes 7 and 37) and can also be started manually with **Run workflow**. It refreshes wildfire, flood, and hurricane/severe-storm snapshots, rebuilds the risk index, and rejects missing, stale, or contract-invalid output with `data/validate_live_outputs.py`.

Successful runs upload a `live-hazard-data-<run-id>` Actions artifact retained for two days. This artifact is for inspection and the later Pages-deployment step; it does not update the published site yet. GitHub only schedules workflows from the default branch, so the schedule becomes active after this workflow is merged there.

The exploration script saves raw GeoJSON and CSV files under `data/raw/geomet/` and `data/explore/`. The pipelines write risk-response JSON and map layers under `data/pipeline/` and `data/samples/`. The frontend discovers risk files through `data/pipeline/risk/index.json`, uses embedded demonstration values when a snapshot is unavailable, and labels stale data after `expiresAt`.

The healthcare pipeline saves cleaned CSVs, regional summaries, and `data/healthcare/facilities.geojson` with facility locations, source information, and timestamps. The Canada overview loads that GeoJSON on demand from the **Health facilities** map control and clusters visible points in the browser. The committed inventory contains 7,525 map-ready records and was last modified at source in 2020; it does not verify current operating status or capacity. Optional hazard analysis requires `geopandas>=1.0` and valid polygons in `data/hazard_zones.geojson`; otherwise exposure is unknown (`null`). It suggests up to three alternatives within 50 km straight-line distance, without verifying safety or capacity.

## Run the frontend

The frontend does not need a build step or package install.

From the repository root, run:

```bash
.venv/bin/python -m http.server 8000
```

Then open:

```text
http://localhost:8000/
```

Open `http://localhost:8000/`; `index.html` launches `ClimateGuard_Mockup.html`. Do not open the HTML through `file://`, because browser `fetch()` cannot reliably load the data files that way.

## Future API connection

The static adapter already translates the HTML hazard names to the contract names. A future API adapter must preserve the same mapping:

| Dashboard value | API value      |
| --------------- | -------------- |
| `wildfire`      | `wildfire`     |
| `rain`          | `flood`        |
| `hurricane`     | `severe_storm` |

A simple request flow should look like this:

1. The user selects a region and hazard in the dashboard.
2. The adapter translates the hazard name.
3. The browser sends a request that matches `risk-request.schema.json`.
4. The backend normalizes provider data and calculates the risk score.
5. The browser renders the response or falls back to the existing mock data.
6. The briefing button sends the normalized assessment to `/api/briefing`.

Do not send provider credentials or LLM keys to the browser. Store secrets in the backend platform's environment settings.

## Suggested next work

1. Generate missing flood layers and refresh expired snapshots.
2. Add automated browser coverage for live, stale, unavailable and empty states.
3. Choose a backend runtime if dynamic endpoints are still required.
4. Implement `/api/risk` using the existing risk-response contract.
5. Implement `/api/briefing` using approved guidance.
6. Test the full demo with the network available and unavailable.

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
