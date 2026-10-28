# ClimateGuard AI

ClimateGuard AI is a two-day hackathon prototype for a **Climate & Emergency Response Intelligence Hub**. It helps emergency teams explore climate-related hazards, understand a simple risk score, and produce a preparedness briefing.

The supplied `ClimateGuard_AI_Interactive_Dashboard.html` is the canonical frontend and stays self-contained. The surrounding project skeleton adds backend integration boundaries, contracts, fixtures, documentation, and tests without replacing or restructuring that file. Its current weather values, risk scores, and recommendations are simulated and are not suitable for real emergency decisions.

Developers continuing the project should start with the [developer guide](docs/developer-guide.md).

## MVP demo flow

1. Select a Canadian region.
2. Switch between wildfire, flood, and severe-storm scenarios.
3. Review the illustrative risk score and supporting indicators.
4. Generate a deterministic preparedness briefing.

## Run locally

No install or build step is required. Open `ClimateGuard_AI_Interactive_Dashboard.html` in a browser, or serve the folder locally:

```bash
python3 -m http.server 8000
```

Then visit `http://localhost:8000/ClimateGuard_AI_Interactive_Dashboard.html`.

## Project structure

```text
.
├── index.html                              # GitHub Pages entry point
├── ClimateGuard_AI_Interactive_Dashboard.html # Canonical supplied frontend
├── api/                                    # Frontend/backend HTTP contract
│   ├── contracts/                          # Versioned JSON Schemas
│   └── examples/                           # Example request/response bodies
├── backend/
│   ├── src/                                # Providers, services, and routes
│   └── tests/                              # Backend unit/contract tests
├── assets/                                 # Optional non-embedded media only
├── data/                                   # Offline demo fixtures
├── docs/                                   # Architecture and demo notes
└── tests/                                  # Frontend manual MVP checks
```

## 48-hour build plan

### Day 1 — prove the core experience

- Finalize the single user story: an emergency planner reviews one hazard for one region.
- Connect one weather source and normalize its response behind a small data adapter.
- Implement the risk endpoint against the agreed JSON contracts.
- Agree on a transparent, rules-based prototype risk formula.
- Keep cached mock data so the demo still works if an API is unavailable.

### Day 2 — add intelligence and polish

- Connect the existing frontend actions to the backend endpoints with the smallest possible adapter inside the supplied HTML.
- Connect an approved LLM endpoint for grounded preparedness summaries.
- Constrain recommendations to vetted emergency-management guidance.
- Add loading, empty, error, and stale-data states.
- Test the demo flow, mobile layout, keyboard controls, and screen-reader labels.
- Record a backup demo and prepare a short architecture/risk slide.

## Suggested architecture

```text
Weather API ─┐
             ├─> normalization layer ─> risk rules ─> dashboard
Hazard data ─┘                                  └─> briefing prompt ─> LLM
```

For a hackathon, the normalization and risk layers can be small serverless functions. Keep API keys server-side; the browser should receive only the normalized fields it needs.

## Frontend rule

Treat `ClimateGuard_AI_Interactive_Dashboard.html` as the presentation layer and source of truth for the UI. `index.html` only launches it for GitHub Pages. Backend work must conform to the contracts in `api/`; avoid moving, splitting, or replacing the dashboard during the two-day build.

## Definition of done

- The happy-path demo completes in under two minutes.
- At least one live data source is visible with its timestamp and attribution.
- The risk score explains its main contributing signals.
- AI output cites or is grounded in an approved source set.
- The interface clearly distinguishes official alerts from prototype guidance.

## After the hackathon

Before production use, add authenticated access, audit logs, regional data governance, validated hazard models, bilingual content, accessibility review, observability, human approval workflows, and formal partnerships with relevant emergency authorities.
