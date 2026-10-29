# Data files for the dashboard

The pipelines in `data/` write plain JSON files. The dashboard can load them with `fetch()` from GitHub Pages; no backend is needed. Every number in them comes from public sources (CWFIS, ECCC GeoMet), and each file says when it was generated.

## Quick reference

| What | Path | Written by |
| --- | --- | --- |
| List of live map layers across Canada (use this) | `data/pipeline/live/manifest.json` (git-ignored; built by the workflow) | `build_live_layers.py` |
| One live map layer | `data/pipeline/live/<layerId>.geojson` | `build_live_layers.py` |
| List of available risk files (use this) | `data/pipeline/risk/index.json` | `build_risk_index.py` |
| Flood and wildfire risk, one region | `data/pipeline/risk/<regionId>_flood.json`, `<regionId>_wildfire.json` | `flood_pipeline.py`, `wildfire_pipeline.py` |
| Hurricane risk, one region | `data/samples/<regionId>_severe_storm.json` | `hurricane_pipeline.py` |
| Wildfire map layers | `data/pipeline/active_fires.geojson`, `active_fire_extents.geojson`, `fire_weather_alerts.geojson` | `wildfire_pipeline.py` |
| Flood map layers | `data/pipeline/stations.geojson`, `river_conditions_latest.geojson`, `active_alerts.geojson` | `flood_pipeline.py` |
| Population density boundaries | `data/pipeline/population_density.geojson` | `build_population_map.py` |
| Population cards and community search | `data/pipeline/population_summary.json` | `build_population_map.py` |
| Hurricane map layers | `data/pipeline/hurricane_tracks_latest.geojson`, `hurricane_historical_storms.geojson`, `hurricane_active_alerts.geojson` (git-ignored; regenerate) | `hurricane_pipeline.py` |
| List of replays | `data/scenarios/index.json` | `build_replay.py` |
| One replay (time steps) | `data/scenarios/<fireId>/timeline.json` | `build_replay.py` |
| Smoke during a replay (AQHI+ per step) | `data/scenarios/<fireId>/air_quality.json` | `air_quality_pipeline.py --replay` |
| Reception centres for a replay | `data/scenarios/<fireId>/resources.json` | `build_reception_centres.py` |
| Live smoke (AQHI) per region | `data/pipeline/air_quality/<regionId>_aqhi.json` (refreshed by the workflow) | `air_quality_pipeline.py` |
| Past disasters per region and hazard (all 12 regions) | `data/history/disaster_history.json` | `disaster_history.py` |
| Health facilities (ODHF) | `data/healthcare/facilities.geojson` | `healthcare_data_pipeline.py` |
| Regions | `data/scenarios.json` | edited by hand |

## Live risk (one file per region and tab)

File name: `<regionId>_<hazard>.json`, for example `clinton_wildfire.json` or `calgary_flood.json`. Flood and wildfire files are in `data/pipeline/risk/`; the hurricane pipeline writes `data/samples/<regionId>_severe_storm.json`. Don't build the folder into the frontend: look the file up in `data/pipeline/risk/index.json`, whose `path` field (relative to `data/`) covers both. Each file is a `risk-response` (see `api/contracts/risk-response.schema.json`), the same shape the planned `POST /api/risk` will return, so the frontend code won't change when a real API exists.

Dashboard tab to `hazard` (from `api/README.md`):

| Tab key in the HTML | `hazard` in the file name |
| --- | --- |
| `wildfire` | `wildfire` |
| `rain` | `flood` |
| `hurricane` | `severe_storm` |

Region ids come from `data/scenarios.json`: `vancouver`, `calgary`, `halifax`, `clinton`, `west-kelowna`. Each region has `lat`/`lon` for the map.

The dashboard selector uses these same five city-region ids, so a selected region can be matched directly against the risk index.

What to show from a risk file:

- `assessment.score` (0-100) and `assessment.level` (`low`, `moderate`, `high`, `critical`)
- `assessment.summary`: one sentence, ready to display
- `assessment.factors[]`: the breakdown, each with `label`, `value`, `unit`, `contribution` (points) and `explanation`; good for a "why this score" panel
- `sources[]`: name, URL and `observedAt` for citations
- `generatedAt` / `expiresAt`: show "as of ..." and flag the data as stale after `expiresAt`
- `dataMode`: `live` for pipeline snapshots, `cached` for replays
- `disclaimer`: show it near the score

`data/pipeline/risk/index.json` lists every file with its region, hazard, score, level, time and `path`, so the dashboard can find any file, show what's available, or colour a region list without loading every file.

## Live map layers (all of Canada)

The dashboard map shows these layers. The regional map layers in the next section only contain features near the five regions.

`build_live_layers.py` writes one GeoJSON file for each map control in `data/pipeline/live/`. It also writes `manifest.json`, which lists the layers in the order of the map controls. Load the manifest first. Then load each layer from `data/<path>`.

| Layer id | Contents | Source |
| --- | --- | --- |
| `active_fires` | Active wildfires. Fires that are out, or that have no update for 60 days, are removed. | CWFIS (NRCan) |
| `fire_weather_alerts` | Heat, smoke, air quality and fire alerts | ECCC weather alerts |
| `river_conditions` | Each real-time river station that reported in the last 6 h, with the latest reading, the trend and the flood status | ECCC hydrometric data |
| `rain_alerts` | Rain, flood, hurricane, tropical-storm and other weather alerts | ECCC weather alerts |
| `hurricane_forecast` | Forecast tracks, error cones and storm centres. The `kind` property is `track`, `cone` or `centre`. | ECCC Canadian Hurricane Centre |
| `storm_surge_alerts`, `thunderstorm_alerts`, `tornado_alerts`, `snowstorm_alerts`, `wind_alerts` | Alerts of one type | ECCC weather alerts |

Each alert is in one layer only. The first match in `ALERT_CATEGORIES` sets the layer. For example, a "snow squall warning" is a snowstorm alert and not a wind alert. Alert features keep the alert properties without the long text, and add a `category` property.

Each manifest entry has these fields:

- `id`, `label` and `path` (relative to `data/`).
- `status`: `ok` (refreshed in this run), `stale` (the refresh failed, so the previous file stays), or `error` (the refresh failed and no previous file exists, so the layer is empty).
- `featureCount`.
- `observedAt`: the newest observation or publication in the layer. An empty layer uses the query time.
- `generatedAt` and `expiresAt`: `expiresAt` is 90 minutes after `generatedAt`. Show the layer as stale after `expiresAt`.
- `source`: `name` and `url`.
- `error`: the error message, or `null`.

The river status needs the history of each station. The histories are kept in `data/pipeline/baselines.json`. Each run gets a maximum of 800 missing histories (`--baseline-budget`), so the full cache fills after approximately four runs. Until then, a station has `"baseline": "pending"` and `"status": "unknown"`.

## Wildfire map layers

- `active_fires.geojson`: one point per active fire near a region. Properties include `agency_fire_id`, `stage` (`OC` out of control, `BH` being held, `UC` under control), `severity` 1-4, `size_ha`, `radius_km`, `distance_km`, `edge_distance_km`, `region_id`.
- `active_fire_extents.geojson`: a circle polygon per fire with the reported area. **Label it "approximate extent"**: it is not a mapped perimeter.
- `fire_weather_alerts.geojson`: ECCC heat, smoke, air quality and wind alerts, with their real polygons.

## Replays (time slider)

1. Load `data/scenarios/index.json`. `replays[]` has, per replay: `scenarioId`, `label`, `region` (id, label, lat, lon), `start`, `end`, `stepHours`, `stepCount`, `keyMoments`, `referenceEvents` and `file`.
2. Load `data/scenarios/<file>` (for example `2023_BC_2023-K52767/timeline.json`).
3. `steps[i]` is one slider position:
   - `t`: time of the step (UTC)
   - `isReported`: `false` means the fire size was interpolated between agency reports; worth a small "interpolated" tag
   - `fire`: `sizeHa`, `stage`, `stageLabel`, `radiusKm`, `edgeDistanceKm`, `lat`, `lon`, `geometry` (circle polygon; approximate)
   - `risk`: a full `risk-response`, the same as a live file, so the live risk panel can render it unchanged

Two replays are built:

| Replay | Region | Steps |
| --- | --- | --- |
| McDougall Creek wildfire, Aug 2023 | `west-kelowna` | every 6 h |
| Pear Lake wildfire, Jul 2026 | `clinton` | every 12 h |

`metadata.keyMoments` lists when the risk level changes (good slider tick marks). `metadata.referenceEvents` lists what really happened, from news reports, with `date`, `timeLocal` (BC time, may be null), `event` and `source` URL. Show these as markers on the slider to compare the hub's risk level with real evacuations. Note: `t` is UTC and `referenceEvents` are BC local time (UTC-7 in summer).

`metadata.assumptions` lists what is real and what is derived. Show it in an info popover.

## Loading example

```js
const HAZARD = { wildfire: 'wildfire', rain: 'flood', hurricane: 'severe_storm' };

let riskIndex = null;

async function loadRisk(regionId, tab) {
  try {
    riskIndex ??= await (await fetch('data/pipeline/risk/index.json')).json();
    const entry = riskIndex.entries.find(e => e.regionId === regionId && e.hazard === HAZARD[tab]);
    if (!entry) return null;                       // null -> keep the built-in mock data
    const r = await fetch(`data/${entry.path}`);
    return r.ok ? await r.json() : null;
  } catch {
    return null;
  }
}

async function loadReplay(scenarioId) {
  const index = await (await fetch('data/scenarios/index.json')).json();
  const entry = index.replays.find(r => r.scenarioId === scenarioId);
  return (await fetch(`data/scenarios/${entry.file}`)).json();
}
```

If a file is missing or fails to load, keep the current mock values and show "Demonstration mode", as `docs/demo-script.md` describes.

**Local testing:** `fetch()` does not work when the HTML is opened straight from disk (`file://`). Serve the folder with `python3 -m http.server 8000` and open `http://localhost:8000/...`, or use the GitHub Pages site.

## Refreshing the data

From the repository root (see `docs/developer-guide.md` for the `.venv` setup):

```bash
.venv/bin/python data/flood_pipeline.py
.venv/bin/python data/wildfire_pipeline.py
.venv/bin/python data/hurricane_pipeline.py
.venv/bin/python data/build_live_layers.py  # Canada-wide map layers
.venv/bin/python data/build_risk_index.py   # run after the pipelines
.venv/bin/python data/build_replay.py       # only when replays change; uses saved history, works offline
```

The scheduled workflow does these steps every 30 minutes and deploys the result to GitHub Pages. Do not commit the generated files for a normal refresh. Commit the files in `data/scenarios/` when the replays change.

## What the dashboard uses where

- **Overview, Resource planning panel** (selected region and hazard): population from `population_summary.json`; typical evacuation size, its 75th percentile and similar past events from `disaster_history.json`; the three nearest hospitals from `facilities.geojson`; possible host communities (towns of 10,000+ people 30–250 km away) from `population_summary.json`. The share of evacuees needing group lodging is an adjustable planning assumption (20% by default), labelled on screen.
- **Situation tab, wildfire replay** (Clinton and West Kelowna): `timeline.json`, `air_quality.json` and `resources.json` for that fire; see `docs/air-quality-and-shelters.md`.
- A region with no risk snapshot yet shows **"No data yet"**. The dashboard has no simulated fallback scores.
- `disaster_history.py` rebuilds from `data/history/cdd_events_clean.csv` when the raw CDD extract is not in `data/raw/cdd/`, so new regions can be added without the original spreadsheet.


## AI briefings

`data/pipeline/live/briefings.json` (built by the workflow, published with the site) is written by `data/build_briefings.py`:

- **Model:** Qwen2.5 7B (open source, Apache 2.0), run by [Ollama](https://ollama.com) on the workflow's own runner (`.github/scripts/start_briefing_model.sh`) and called through Ollama's OpenAI-compatible API at `localhost`. No API key, no outside AI service, nothing leaves the runner. The model is cached between runs. GitHub Models, used before, was retired on July 30, 2026. `BRIEFING_ENDPOINT`, `BRIEFING_API_KEY` and `BRIEFING_MODELS` point the script at another OpenAI-compatible service.
- **Subjects:** every region × hazard in `risk/index.json`, and every key moment of both wildfire replays.
- **Grounding:** the model receives only numbered facts built from our files (score and factors, smoke, disaster history, nearest hospitals, possible host communities, population; for replays also the fire, reception centres and reported events) and must cite them as `[F2]`. Every number it writes must appear in the facts; otherwise it is asked to fix the answer once, then rejected.
- **Format:** each entry has `briefing` (the `api/contracts/briefing-response.schema.json` shape; `requiresHumanReview` is always true), `publicAlert.en` and `publicAlert.fr`, the `facts` it was given, `model`, `generatedAt`, `score`, `level` and `asOf`.
- **Run time:** the model runs on CPU, a few minutes per answer, so each refresh makes at most 4 model calls (`BRIEFING_MAX_CALLS`) and none after 12 minutes (`BRIEFING_TIME_BUDGET`). Replay briefings are written once; live ones are rewritten only when the score or level changes or after 12 hours. Earlier briefings are reused from the published site.
- **Replay briefings, generated once:** the `Generate replay briefings` workflow (Actions tab, or a change to its file on its branch) writes every replay moment with the same model, saves the accepted drafts with `pin_replay_briefings.py` and commits them to the branch it ran on. It never deploys or touches `main`.
- **Fallback:** a subject without a valid AI briefing gets a template briefing from the same facts (`model: "template"`), so the dashboard always shows accurate text.
- **Saved replay briefings:** `python data/pin_replay_briefings.py` copies the accepted replay AI drafts from the published site into `data/scenarios/<fireId>/briefings.json`. Once committed, `build_briefings.py` uses them as they are (`pinned: true`), with no model call and no template fallback, so the replay never depends on a live AI run. Edit the text by hand if needed and set `"reviewed": true` on drafts a person has checked; the dashboard then says "reviewed by the team". Run it again to add moments that were still templates (existing drafts are kept unless you pass `--replace`).
- **Places to watch:** the top 8 places to watch (moderate or above) get a briefing too (key `watch/<id>`), redrafted only when their level changes or after 12 hours.
- **Dashboard:** the Briefing & advisory tab shows the briefing for the selected region and hazard, or for the current replay step (with the facts list and an EN/FR public advisory draft). The replay summary strip and the risk card's "Recommended next step" use it when an AI briefing exists.
