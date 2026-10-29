# ClimateGuard AI

ClimateGuard AI is a two-day hackathon prototype for a **Climate & Emergency Response Intelligence Hub**. It helps emergency teams explore climate-related hazards, understand a simple risk score, and produce a preparedness briefing.

`ClimateGuard_Mockup.html` is the canonical frontend and the root `index.html` opens it on GitHub Pages. Live data, risk scores and AI briefings are built from public Canadian data by a GitHub Actions workflow every 30 minutes and published with the site. Two wildfire replays (McDougall Creek 2023, Pear Lake 2026) show the same scoring and briefings on real past events. The What-if and Plan check tabs use a labelled sample scenario. The prototype is not suitable for real emergency decisions.

Developers continuing the project should start with the [developer guide](docs/developer-guide.md). Data files are described in [docs/data-files.md](docs/data-files.md).

## Demo flow

1. **Overview:** live wildfires and river stations across Canada, and the risk score for the selected region and hazard, with the factors behind it.
2. **Places to watch:** every community of 1,000+ people screened with the same rules, grouped by the fire or river driving the risk. Select a row or a map marker to see the communities affected and the briefing.
3. **Resource planning:** population, typical evacuation size from past disasters, group lodging, host communities and nearest hospitals for the selected region.
4. **Replay:** select Clinton (Pear Lake fire) or West Kelowna (McDougall Creek fire) and open the replay. The score, map, smoke readings, reception centres and AI briefing change as the fire grows, next to what actually happened.
5. **Briefing & advisory:** the full briefing with its numbered facts and sources, and a public advisory draft in English and French.

## How it works

```text
Public data (NRCan, ECCC, Statistics Canada, ...)            every 30 minutes, GitHub Actions
   │
   ├─ pipelines (data/*_pipeline.py) ──> risk score per region and hazard (0-100, with factors)
   ├─ build_live_layers.py ────────────> Canada-wide map layers
   ├─ build_watchlist.py ──────────────> places to watch (every community of 1,000+ people)
   └─ build_briefings.py ──> numbered facts ──> AI draft (Qwen2.5 7B, Ollama) ──> checks ──> briefing
                                                         └─ fails or unavailable ──> template from the same facts
   │
   └─> published with the site on GitHub Pages; ClimateGuard_Mockup.html reads the JSON files
```

**Risk scores are rules, not AI.** Each hazard has a transparent 0-100 score built from named factors, and the dashboard shows every factor's points. Wildfire: distance to the nearest fire's edge (40), uncontained fire within 5 km (20), uncontained fires within 150 km (10), burned area (10), fire-weather alerts (10) and past large fires (10). Flood, from river stations within 50 km: highest reading compared with the station's own history for the month (50), highest flow as a share of its typical yearly peak (30), above-normal stations still rising (10) and rain, flood or storm-surge alerts (10). Levels: under 25 low, 25-49 moderate, 50-74 high, 75 and over critical.

**Places to watch** applies the same rules to every community of 1,000+ people in Canada and groups them by the fire or river station driving the risk. River readings are checked before they count: level-only gauges, flat placeholder readings, single spikes, short records and seasonal trickles far below flood flows are not treated as floods.

**Replays** rebuild each moment of a real fire from the agency reports available at that time (CWFIS), with smoke readings (BC air quality), the reception centres that actually opened and the events that actually happened, from news and official reports. The score uses the same rules as the live view.

**AI briefings.** For each region, each top place to watch and each key replay moment, the pipeline writes a list of numbered facts (score, factors, fire or river status, population, past disasters, hospitals, host communities, open reception centres) and asks the model for a situation summary, 3-5 preparedness actions and a public advisory in English and French. Before a draft is accepted, the code checks that:
- every number in it appears in the facts (no invented numbers);
- the situation cites facts like [F2], and the dashboard links each citation to its fact;
- the actions fit the level (no evacuations or reception-centre openings at low or moderate risk);
- it has the expected structure.

A draft that fails is retried once with the problems listed. If it still fails, or the AI service is unavailable, a template briefing is built from the same facts, so the dashboard always shows accurate text and says which kind it is. Every briefing is marked "needs human review"; nothing is sent to the public automatically. The replay briefings are saved in the repo once the team has reviewed them (`data/pin_replay_briefings.py`), so the replays never depend on a live AI call.

## AI and third-party services (disclosure)

| What | Used for | Cost and access |
|---|---|---|
| **Qwen2.5 7B** (Alibaba Qwen, open-source model, Apache 2.0) run by **Ollama** (open source) | Drafting briefings and public advisories from our numbered facts | Free: runs on the GitHub Actions runner itself; no API key and no outside AI service, nothing leaves the runner. Replaces GitHub Models (OpenAI GPT-4.1-mini), which GitHub retired on July 30, 2026. |
| NRCan Canadian Wildland Fire Information System (CWFIS) | Active and past wildfires | Free, public |
| ECCC MSC GeoMet (api.weather.gc.ca) and Water Survey of Canada | River levels and flows, weather alerts, AQHI air quality, hurricane tracks | Free, public |
| BC Ministry of Environment air quality data | Smoke readings (PM2.5) for the replays | Free, public |
| Statistics Canada | Population estimates and census boundaries | Free, public |
| Open Database of Healthcare Facilities (Statistics Canada) | Hospitals and health facilities | Free, public |
| Public Safety Canada, Canadian Disaster Database | Past disasters and evacuation sizes | Free, public |
| NOAA IBTrACS | Historical hurricane tracks | Free, public |
| News and official reports (EmergencyInfoBC, local news) | What actually happened during the replayed fires | Linked as sources |
| CARTO, OpenTopoMap, Esri World Imagery | Map background tiles | Free tiers |
| Leaflet, d3-geo, topojson-client | Map and boundaries in the browser (open source, bundled in the page) | Open source |
| GitHub Pages and GitHub Actions | Hosting and the 30-minute refresh | Free for public repositories |

No paid services, no personal data and no internal government data are used.

## Run locally

No build step is required. Serve the folder locally so browser `fetch()` can load the data files:

```bash
python3 -m http.server 8000
```

Then visit `http://localhost:8000/`.

## Project structure

```text
.
├── index.html                              # GitHub Pages entry point
├── ClimateGuard_Mockup.html                # Canonical frontend
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

Treat `ClimateGuard_Mockup.html` as the presentation layer and source of truth for the UI. `index.html` launches it for GitHub Pages. Data adapters should continue to conform to the contracts in `api/`.

## Definition of done

- The happy-path demo completes in under two minutes.
- At least one live data source is visible with its timestamp and attribution.
- The risk score explains its main contributing signals.
- AI output cites or is grounded in an approved source set.
- The interface clearly distinguishes official alerts from prototype guidance.

## After the hackathon

Before production use, add authenticated access, audit logs, regional data governance, validated hazard models, bilingual content, accessibility review, observability, human approval workflows, and formal partnerships with relevant emergency authorities.
