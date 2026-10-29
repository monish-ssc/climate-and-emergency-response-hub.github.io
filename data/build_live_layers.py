"""
build_live_layers.py - Canada-wide live map layers and a manifest that describes them.

The regional pipelines (wildfire, flood, hurricane) only keep features near the five scenario
regions. This script writes national layers for the map, one GeoJSON file per map control:

  active_fires          CWFIS active wildfires (out, and stale, records dropped)
  fire_weather_alerts   ECCC heat, smoke, air quality and fire alerts
  river_conditions      ECCC real-time river stations, latest reading, trend and flood status
  rain_alerts           ECCC rain, flood, tropical-storm and other weather alerts
  hurricane_forecast    ECCC Canadian Hurricane Centre tracks, error cones and storm centres
  storm_surge_alerts, thunderstorm_alerts, tornado_alerts, snowstorm_alerts, wind_alerts

data/pipeline/live/manifest.json lists every layer with its status, featureCount, observedAt,
generatedAt, expiresAt and source. "path" is relative to data/, like risk/index.json.

Each source is fetched once and a failed source does not stop the other layers. When a source
fails, the previous file is kept and its layer is marked "stale"; with no previous file, an
empty layer is written and marked "error". The workflow restores the previous files from the
published site first (restore_published_snapshot.py), so "previous" means the last deployment.

River status needs each station's history. Histories are cached in data/pipeline/baselines.json
(shared with flood_pipeline.py) and at most --baseline-budget missing stations are fetched per
run, so the national cache fills over a few runs. Until then a station's status is "unknown".
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

if __package__:
    from . import flood_pipeline as flood
    from . import hurricane_pipeline as hurricane
    from . import wildfire_pipeline as wildfire
else:
    import flood_pipeline as flood
    import hurricane_pipeline as hurricane
    import wildfire_pipeline as wildfire

DATA_DIR = Path(__file__).resolve().parent
LIVE_DIR = DATA_DIR / "pipeline" / "live"
MANIFEST = LIVE_DIR / "manifest.json"
MANIFEST_VERSION = "1.0"
VALID_MINUTES = 90  # refresh runs every 30 min; allow for two missed or delayed runs
RIVER_HOURS = 6  # real-time readings fetched per station (the trend compares 3 h windows)
BASELINE_BUDGET = 800  # missing station histories fetched per run (about 2 minutes)
COORD_DIGITS = 4  # about 10 m; keeps alert polygons small

ALERT_KEEP = (
    "alert_code", "alert_name_en", "alert_short_name_en", "alert_type", "risk_colour_en",
    "confidence_en", "impact_en", "status_en", "province", "feature_id", "feature_name_en",
    "publication_datetime", "validity_datetime", "event_end_datetime", "expiration_datetime",
)
# First match wins, so "snow squall" is a snowstorm and "freezing rain" is not rain.
ALERT_CATEGORIES = (
    ("tornado", ("tornado",)),
    ("thunderstorm", ("thunderstorm",)),
    ("storm_surge", ("storm surge",)),
    ("tropical", ("hurricane", "tropical storm")),
    ("fire_weather", ("heat", "air quality", "smoke", "fire")),
    ("snowstorm", ("snow", "blizzard", "winter storm", "freezing", "flash freeze")),
    ("wind", ("wind", "squall", "arctic outflow", "dust")),
    ("rain", ("rain", "flood")),
)
ALERT_LAYERS = {  # layer id -> alert categories in it
    "fire_weather_alerts": ("fire_weather",),
    "rain_alerts": ("rain", "tropical", "other"),
    "storm_surge_alerts": ("storm_surge",),
    "thunderstorm_alerts": ("thunderstorm",),
    "tornado_alerts": ("tornado",),
    "snowstorm_alerts": ("snowstorm",),
    "wind_alerts": ("wind",),
}
GEOMET_ALERTS = {"name": "ECCC Weather alerts", "url": f"{flood.API}/weather-alerts"}
LAYERS = (  # id, label, source; the order is the order of the map controls
    ("active_fires", "Active wildfires",
     {"name": "CWFIS Active wildfires (NRCan)", "url": f"{wildfire.CWFIS_WFS}?typeName={wildfire.ACTIVE_LAYER}"}),
    ("fire_weather_alerts", "Fire-weather alerts", GEOMET_ALERTS),
    ("river_conditions", "River stations and flooding conditions",
     {"name": "ECCC Real-time hydrometric data", "url": f"{flood.API}/hydrometric-realtime"}),
    ("rain_alerts", "Rain and severe-weather alerts", GEOMET_ALERTS),
    ("hurricane_forecast", "Hurricane forecast tracks",
     {"name": "ECCC Canadian Hurricane Centre forecasts", "url": f"{flood.API}/hurricanes-track-realtime"}),
    ("storm_surge_alerts", "Storm-surge alerts", GEOMET_ALERTS),
    ("thunderstorm_alerts", "Thunderstorm alerts", GEOMET_ALERTS),
    ("tornado_alerts", "Tornado alerts", GEOMET_ALERTS),
    ("snowstorm_alerts", "Snowstorm and winter alerts", GEOMET_ALERTS),
    ("wind_alerts", "Wind alerts", GEOMET_ALERTS),
)
NO_BASELINE = {m: {"month_means": [], "median_annual_peak": None} for m in ("discharge", "level")}


def parse_time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else None


def latest(values, fallback):
    """Newest timestamp in values as ISO 8601 UTC; fallback (the query time) when there is none."""
    times = [t for t in map(parse_time, values) if t]
    return flood.iso(max(times)) if times else fallback


def round_coords(coords):
    if isinstance(coords, (int, float)):
        return round(coords, COORD_DIGITS)
    return [round_coords(c) for c in coords]


def feature(geometry, properties):
    geometry = dict(geometry)
    geometry["coordinates"] = round_coords(geometry["coordinates"])
    return {"type": "Feature", "geometry": geometry, "properties": properties}


# ---------- wildfires ----------


def fire_layer(now):
    fires, _ = wildfire.fetch_active_fires(now)
    features = []
    for f in fires:
        if f["_lat"] is None or f["_lon"] is None:
            continue
        props = {k: v for k, v in f.items() if not k.startswith("_")}
        features.append(feature({"type": "Point", "coordinates": [f["_lon"], f["_lat"]]}, props))
    observed = latest((f["properties"]["status_date"] for f in features), flood.iso(now))
    return {"active_fires": (features, observed)}


# ---------- weather alerts ----------


def alert_category(name):
    name = (name or "").lower()
    return next((cat for cat, words in ALERT_CATEGORIES if any(w in name for w in words)), "other")


def alert_layers(now):
    """One Canada-wide request, split into one layer per map control."""
    newest = {}
    for f in flood.fetch("weather-alerts"):
        p = f.get("properties") or {}
        expires = parse_time(p.get("expiration_datetime"))
        if not f.get("geometry") or p.get("status_en") == "ended" or (expires and expires <= now):
            continue
        # one feature per alert per forecast zone; keep the newest publication of each
        key = (p.get("alert_code"), p.get("alert_type"), p.get("feature_id"))
        if key not in newest or (p.get("publication_datetime") or "") > (
            newest[key]["properties"].get("publication_datetime") or ""
        ):
            newest[key] = f
    by_category = {}
    for f in newest.values():
        p = f["properties"]
        props = {k: p.get(k) for k in ALERT_KEEP}
        props["category"] = alert_category(p.get("alert_name_en"))
        by_category.setdefault(props["category"], []).append(feature(f["geometry"], props))
    out = {}
    for layer_id, categories in ALERT_LAYERS.items():
        features = [f for c in categories for f in by_category.get(c, [])]
        observed = latest((f["properties"]["publication_datetime"] for f in features), flood.iso(now))
        out[layer_id] = (features, observed)
    return out


# ---------- rivers ----------


def update_baselines(stations, month, budget):
    """Station histories from the shared cache; fetch at most `budget` missing ones and save."""
    path = flood.BASELINE_CACHE
    cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    missing = [s for s in stations if cache.get(s, {}).get("month") != month][:budget]
    if missing:
        print(f"  fetching history for {len(missing)} stations ...")

        def fetch_one(stn):
            try:
                return stn, flood.fetch_baseline(stn, month)
            except Exception as e:  # noqa: BLE001 - one station must not stop the layer
                print(f"  history unavailable for {stn}: {e}")
                return stn, None

        with ThreadPoolExecutor(max_workers=8) as pool:
            for stn, base in pool.map(fetch_one, missing):
                if base is not None:
                    cache[stn] = base
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
    ready = sum(cache.get(s, {}).get("month") == month for s in stations)
    print(f"  history ready for {ready} of {len(stations)} reporting stations")
    return cache


def river_layer(now, budget):
    stations = flood.to_frame(flood.fetch(
        "hydrometric-stations", STATUS_EN="Active", REAL_TIME=1,
        properties="STATION_NUMBER,STATION_NAME,PROV_TERR_STATE_LOC,DRAINAGE_AREA_GROSS",
    ))
    readings = flood.to_frame(flood.fetch(
        "hydrometric-realtime", datetime=f"{flood.iso(now - timedelta(hours=RIVER_HOURS))}/..",
        properties="STATION_NUMBER,DATETIME,LEVEL,DISCHARGE", skipGeometry="true",
    ))
    if stations.empty or readings.empty:
        raise RuntimeError("hydrometric stations or real-time readings returned no data")
    # offset paging over a growing collection can repeat a record
    readings = readings.drop_duplicates(["STATION_NUMBER", "DATETIME"])
    readings = readings[readings[["LEVEL", "DISCHARGE"]].notna().any(axis=1)]
    stations = stations.dropna(subset=["_lat", "_lon"]).drop_duplicates("STATION_NUMBER")
    stations = stations.set_index("STATION_NUMBER")
    readings = readings[readings["STATION_NUMBER"].isin(stations.index)]
    reporting = sorted(readings["STATION_NUMBER"].unique())
    print(f"River stations: {len(stations)} active real-time, {len(reporting)} reporting "
          f"in the last {RIVER_HOURS} h ({len(readings)} readings)")
    cache = update_baselines(reporting, now.month, budget)

    features = []
    for stn, group in readings.groupby("STATION_NUMBER"):
        base = cache.get(stn)
        ready = bool(base) and base.get("month") == now.month
        c = flood.assess_station(group, base if ready else NO_BASELINE, window_hours=RIVER_HOURS / 2)
        s = stations.loc[stn]
        props = {
            "station_number": stn,
            "station_name": s["STATION_NAME"],
            "province": s["PROV_TERR_STATE_LOC"],
            "drainage_area_km2": None if pd.isna(s["DRAINAGE_AREA_GROSS"]) else float(s["DRAINAGE_AREA_GROSS"]),
            "metric": c["metric"],
            "unit": c["unit"],
            "value": c["value"],
            "observed_at": c["observed_at"],
            "recent_mean": c["mean_24h"],
            "readings": c["readings_24h"],
            "window_hours": RIVER_HOURS,
            "trend": c["trend"],
            "change": c["change_12h_vs_prev_12h"],
            "change_unit": c["change_unit"],
            "percentile_for_month": c["percentile_for_month"],
            "history_years": c["history_years"],
            "median_annual_peak": c["median_annual_peak"],
            "pct_of_flood_peak": c["pct_of_flood_peak"],
            "status": c["status"],
            "note": c.get("note"),
            "baseline": "ready" if ready else "pending",
        }
        features.append(feature({"type": "Point", "coordinates": [float(s["_lon"]), float(s["_lat"])]}, props))
    observed = latest((f["properties"]["observed_at"] for f in features), flood.iso(now))
    return {"river_conditions": (features, observed)}


# ---------- hurricanes ----------


def hurricane_props(p, kind):
    props = {
        "kind": kind,
        "storm_name": p.get("storm_name"),
        "basin": p.get("basin"),
        "storm_number": p.get("storm_number"),
        "publication_datetime": p.get("publication_datetime"),
    }
    if kind == "centre":
        props.update({
            "forecast_datetime": p.get("forecast_datetime"),
            "classification": p.get("metobject.classification"),
            "storm_type": p.get("metobject.sub_type"),
            "max_wind_kt": p.get("metobject.max_wind.value"),
            "pressure_mbar": p.get("metobject.pressure.value"),
        })
    return props


def hurricane_layer(now):
    features = []
    for collection, kind in (
        ("hurricanes-error_cone-realtime", "cone"),
        ("hurricanes-track-realtime", "track"),
        ("hurricanes-cyclone-realtime", "centre"),
    ):
        for f in hurricane.fetch(collection, active="true", latest_publication="true"):
            p = f.get("properties") or {}
            published = parse_time(p.get("publication_datetime"))
            # unlike the regional screening, a stale product is dropped here, not a failure
            if not f.get("geometry") or not published or now - published > hurricane.MAX_TRACK_AGE:
                continue
            features.append(feature(f["geometry"], hurricane_props(p, kind)))
    observed = latest((f["properties"]["publication_datetime"] for f in features), flood.iso(now))
    return {"hurricane_forecast": (features, observed)}


# ---------- manifest ----------


def write_layer(path, features):
    doc = {"type": "FeatureCollection", "features": features}
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(doc, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def previous_entries():
    if not MANIFEST.exists():
        return {}
    try:
        return {e["id"]: e for e in json.loads(MANIFEST.read_text(encoding="utf-8"))["layers"]}
    except (ValueError, KeyError, TypeError):
        return {}


def build(now, budget=BASELINE_BUDGET):
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    previous = previous_entries()
    results, errors = {}, {}
    builders = (
        (("active_fires",), lambda: fire_layer(now)),
        (tuple(ALERT_LAYERS), lambda: alert_layers(now)),
        (("river_conditions",), lambda: river_layer(now, budget)),
        (("hurricane_forecast",), lambda: hurricane_layer(now)),
    )
    for layer_ids, run in builders:
        try:
            results.update(run())
        except Exception as e:  # noqa: BLE001 - one failed source must not stop the others
            print(f"  FAILED {', '.join(layer_ids)}: {e}")
            errors.update({i: str(e)[:300] for i in layer_ids})

    generated, expires = flood.iso(now), flood.iso(now + timedelta(minutes=VALID_MINUTES))
    entries = []
    for layer_id, label, source in LAYERS:
        path = LIVE_DIR / f"{layer_id}.geojson"
        entry = {
            "id": layer_id, "label": label, "path": path.relative_to(DATA_DIR).as_posix(),
            "source": source,
        }
        if layer_id in results:
            features, observed = results[layer_id]
            write_layer(path, features)
            entry.update(status="ok", featureCount=len(features), observedAt=observed,
                         generatedAt=generated, expiresAt=expires, error=None)
        elif path.exists() and layer_id in previous:
            old = previous[layer_id]
            entry.update(status="stale", featureCount=old.get("featureCount"),
                         observedAt=old.get("observedAt"), generatedAt=old.get("generatedAt"),
                         expiresAt=old.get("expiresAt"), error=errors.get(layer_id, "no data returned"))
        else:
            write_layer(path, [])
            entry.update(status="error", featureCount=0, observedAt=None, generatedAt=generated,
                         expiresAt=expires, error=errors.get(layer_id, "no data returned"))
        entries.append(entry)
    manifest = {
        "manifestVersion": MANIFEST_VERSION,
        "generatedAt": generated,
        "refreshMinutes": 30,
        "layers": entries,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline-budget", type=int, default=BASELINE_BUDGET,
                        help=f"missing station histories to fetch per run (default {BASELINE_BUDGET})")
    args = parser.parse_args()
    manifest = build(datetime.now(timezone.utc).replace(microsecond=0), args.baseline_budget)
    for e in manifest["layers"]:
        print(f"  {e['id']:<21} {e['status']:<6} {e['featureCount'] or 0:>5} features  "
              f"observed {e['observedAt']}")
    print(f"Saved {len(manifest['layers'])} layers and {MANIFEST.relative_to(DATA_DIR.parent)}")
    if all(e["status"] != "ok" for e in manifest["layers"]):
        raise RuntimeError("No live layer could be refreshed")


if __name__ == "__main__":
    main()
