# Data files for the dashboard

The pipelines in `data/` write plain JSON files. The dashboard can load them with `fetch()` from GitHub Pages; no backend is needed. Every number in them comes from public sources (CWFIS, ECCC GeoMet), and each file says when it was generated.

## Quick reference

| What | Path | Written by |
| --- | --- | --- |
| List of available risk files (use this) | `data/pipeline/risk/index.json` | `build_risk_index.py` |
| Flood and wildfire risk, one region | `data/pipeline/risk/<regionId>_flood.json`, `<regionId>_wildfire.json` | `flood_pipeline.py`, `wildfire_pipeline.py` |
| Hurricane risk, one region | `data/samples/<regionId>_severe_storm.json` | `hurricane_pipeline.py` |
| Wildfire map layers | `data/pipeline/active_fires.geojson`, `active_fire_extents.geojson`, `fire_weather_alerts.geojson` | `wildfire_pipeline.py` |
| Flood map layers | `data/pipeline/stations.geojson`, `river_conditions_latest.geojson`, `active_alerts.geojson` | `flood_pipeline.py` |
| Hurricane map layers | `data/pipeline/hurricane_tracks_latest.geojson`, `hurricane_historical_storms.geojson`, `hurricane_active_alerts.geojson` (git-ignored; regenerate) | `hurricane_pipeline.py` |
| List of replays | `data/scenarios/index.json` | `build_replay.py` |
| One replay (time steps) | `data/scenarios/<fireId>/timeline.json` | `build_replay.py` |
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

> **Open question for the team:** the dashboard's built-in region list is provinces (`bc`, `ab`, ...), while the pipelines use these city regions. Either add these regions to the dashboard or decide which region each province shows.

What to show from a risk file:

- `assessment.score` (0-100) and `assessment.level` (`low`, `moderate`, `high`, `critical`)
- `assessment.summary`: one sentence, ready to display
- `assessment.factors[]`: the breakdown, each with `label`, `value`, `unit`, `contribution` (points) and `explanation`; good for a "why this score" panel
- `sources[]`: name, URL and `observedAt` for citations
- `generatedAt` / `expiresAt`: show "as of ..." and flag the data as stale after `expiresAt`
- `dataMode`: `live` for pipeline snapshots, `cached` for replays
- `disclaimer`: show it near the score

`data/pipeline/risk/index.json` lists every file with its region, hazard, score, level, time and `path`, so the dashboard can find any file, show what's available, or colour a region list without loading every file.

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
.venv/bin/python data/build_risk_index.py   # run after the pipelines
.venv/bin/python data/build_replay.py       # only when replays change; uses saved history, works offline
```

Then commit the updated files in `data/pipeline/` and `data/scenarios/`.
