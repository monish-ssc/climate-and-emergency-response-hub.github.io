"""
build_watchlist.py - "Communities to watch": screen every Canadian community of 1,000+ people.

Runs in the refresh workflow after build_live_layers.py and makes no network requests of its own.
It reuses data the workflow has already downloaded:

  data/pipeline/live/active_fires.geojson       CWFIS active fires across Canada
  data/pipeline/live/river_conditions.geojson   ECCC real-time river stations across Canada
  data/pipeline/population_summary.json         Statistics Canada communities with population and centre

For each community it runs the same scoring rules as the regional pipelines:
  wildfire  wildfire_pipeline.score_region on fires within 150 km (no alerts or fire history:
            those need per-community queries, so the screening score is a lower bound)
  flood     flood_pipeline.score_region on river stations within 50 km (no alerts)

Output: data/pipeline/live/watchlist.json, the communities ranked by their highest hazard score
(ties broken by population). Screening only: it points at places to look at, it is not a warning.

Run from the repo root:  python data/build_watchlist.py [--min-population 1000] [--top 25]
"""

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import flood_pipeline as flood
import wildfire_pipeline as wildfire

DATA_DIR = Path(__file__).resolve().parent
LIVE_DIR = DATA_DIR / "pipeline" / "live"
POPULATION = DATA_DIR / "pipeline" / "population_summary.json"
OUT = LIVE_DIR / "watchlist.json"

FIRE_RADIUS_KM = wildfire.RADIUS_KM
RIVER_RADIUS_KM = flood.RADIUS_KM
LEVEL_RANK = {"low": 0, "moderate": 1, "high": 2, "critical": 3}


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_fires(path):
    """Live fire features back into the dicts wildfire_pipeline scores (the layer is clean_fire output)."""
    fires = []
    for f in load(path).get("features", []):
        lon, lat = (f.get("geometry") or {}).get("coordinates", [None, None])[:2]
        p = f.get("properties") or {}
        if lat is None or lon is None or p.get("stage") not in wildfire.STAGE:
            continue
        size = p.get("size_ha")
        fires.append({**p, "_lat": lat, "_lon": lon,
                      "radius_km": p.get("radius_km") or round(wildfire.radius_km(size), 2)})
    return fires


def load_rivers(path):
    out = []
    for f in load(path).get("features", []):
        lon, lat = (f.get("geometry") or {}).get("coordinates", [None, None])[:2]
        if lat is None or lon is None:
            continue
        out.append({**(f.get("properties") or {}), "_lat": lat, "_lon": lon})
    return out


def within(items, lat, lon, km):
    """Cheap bounding-box filter before the exact distance."""
    dlat = km / 111.0
    dlon = km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    for it in items:
        if abs(it["_lat"] - lat) <= dlat and abs(it["_lon"] - lon) <= dlon:
            d = flood.distance_km(lat, lon, it["_lat"], it["_lon"])
            yield it, d


def wildfire_screen(fires, lat, lon):
    candidates = [f for f, _ in within(fires, lat, lon, FIRE_RADIUS_KM + 60)]
    nearby = wildfire.near(candidates, lat, lon, FIRE_RADIUS_KM)
    score, level, _ = wildfire.score_region(nearby, [], None)
    top = nearby[0] if nearby else None
    return {
        "score": score,
        "level": level,
        "firesWithin150Km": len(nearby),
        "nearestFire": None if not top else {
            "id": top.get("agency_fire_id") or top.get("national_fire_id"),
            "stage": top.get("stage_label"),
            "sizeHa": top.get("size_ha"),
            "edgeDistanceKm": top["edge_distance_km"],
            "lat": top["_lat"],
            "lon": top["_lon"],
        },
    }


def flood_screen(rivers, lat, lon):
    conds = []
    for r, d in within(rivers, lat, lon, RIVER_RADIUS_KM):
        if d <= RIVER_RADIUS_KM:
            conds.append({**r, "distance_km": round(d, 1)})
    if not conds:
        return None
    score, level, _ = flood.score_region(conds, [])
    rank = flood.STATUS_RANK
    worst = max(conds, key=lambda c: (rank.index(c.get("status", "unknown")) if c.get("status") in rank else 0,
                                      c.get("percentile_for_month") or 0))
    return {
        "score": score,
        "level": level,
        "stationsWithin50Km": len(conds),
        "worstStation": {
            "name": worst.get("station_name"),
            "number": worst.get("station_number"),
            "lat": worst["_lat"],
            "lon": worst["_lon"],
            "status": worst.get("status"),
            "percentileForMonth": worst.get("percentile_for_month"),
            "trend": worst.get("trend"),
            "distanceKm": worst["distance_km"],
        },
    }


def driver_of(row):
    """What drives a community's lead score: one fire or one river station."""
    h = row["lead"]["hazard"]
    v = row["hazards"][h]
    if h == "wildfire" and v.get("nearestFire"):
        f = v["nearestFire"]
        return ("wildfire", f["id"]), {
            "id": f"fire-{f['id']}",
            "hazard": "wildfire",
            "lat": f.get("lat"),
            "lon": f.get("lon"),
            "label": f"Wildfire {f['id']}",
            "detail": f"{f['stage']}, about {round(f['sizeHa'] or 0):,} ha",
        }
    if h == "flood" and v.get("worstStation"):
        st = v["worstStation"]
        pct = st.get("percentileForMonth")
        return ("flood", st.get("number") or st["name"]), {
            "id": f"river-{st.get('number') or st['name']}",
            "hazard": "flood",
            "lat": st.get("lat"),
            "lon": st.get("lon"),
            "label": (st["name"] or "River station").title(),
            "detail": f"{(st.get('status') or 'unknown').replace('_', ' ')}"
                      + (f", {pct}th percentile for the month" if pct is not None else "")
                      + f", {st.get('trend') or 'trend unknown'}",
        }
    return None, None


def group_hotspots(rows):
    """One entry per fire or river station, with the communities it puts above 'low'."""
    groups = {}
    for r in rows:
        if r["lead"]["level"] == "low":
            continue
        key, info = driver_of(r)
        if key is None:
            continue
        g = groups.setdefault(key, {**info, "score": 0, "level": "low", "population": 0, "communities": []})
        if r["lead"]["score"] > g["score"]:
            g["score"], g["level"] = r["lead"]["score"], r["lead"]["level"]
        g["population"] += int(r["population"] or 0)
        g["communities"].append({"name": r["name"], "province": r["province"], "csduid": r.get("csduid"),
                                 "population": r["population"], "score": r["lead"]["score"],
                                 "lat": r["lat"], "lon": r["lon"]})
    out = sorted(groups.values(), key=lambda g: (-g["score"], -g["population"]))
    for g in out:
        g["communities"].sort(key=lambda c: (-c["score"], -(c["population"] or 0)))
        g["communityCount"] = len(g["communities"])
        g["communities"] = g["communities"][:8]
    return out


def build(min_population=1000, top=25, now=None):
    now = now or datetime.now(timezone.utc)
    pop = load(POPULATION)
    communities = [c for c in pop["communities"]
                   if (c.get("population") or 0) >= min_population
                   and c.get("centre_lat") is not None and c.get("centre_lon") is not None]
    fires_path, rivers_path = LIVE_DIR / "active_fires.geojson", LIVE_DIR / "river_conditions.geojson"
    fires = load_fires(fires_path) if fires_path.exists() else None
    rivers = load_rivers(rivers_path) if rivers_path.exists() else None
    if fires is None and rivers is None:
        raise SystemExit("No live layers found; run build_live_layers.py first.")

    rows, counts = [], {h: {k: 0 for k in LEVEL_RANK} for h in ("wildfire", "flood")}
    for c in communities:
        lat, lon = c["centre_lat"], c["centre_lon"]
        hazards = {}
        if fires is not None:
            hazards["wildfire"] = wildfire_screen(fires, lat, lon)
        if rivers is not None:
            fl = flood_screen(rivers, lat, lon)
            if fl:
                hazards["flood"] = fl
        for h, v in hazards.items():
            counts[h][v["level"]] += 1
        if not hazards:
            continue
        lead = max(hazards, key=lambda h: hazards[h]["score"])
        rows.append({
            "csduid": c.get("csduid"),
            "name": c["name"],
            "province": c.get("province"),
            "population": c.get("population"),
            "lat": lat,
            "lon": lon,
            "lead": {"hazard": lead, "score": hazards[lead]["score"], "level": hazards[lead]["level"]},
            "hazards": hazards,
        })
    rows.sort(key=lambda r: (-r["lead"]["score"], -(r["population"] or 0)))
    hotspots = group_hotspots(rows)
    doc = {
        "generatedAt": iso(now),
        "dataMode": "live",
        "method": ("Screening of every community with at least "
                   f"{min_population:,} people: wildfire score from active fires within {FIRE_RADIUS_KM} km "
                   f"and flood score from river stations within {RIVER_RADIUS_KM} km, using the same rules as "
                   "the regional scores. Weather alerts and fire history are not included, so scores are a "
                   "lower bound. Screening only, not a warning."),
        "minPopulation": min_population,
        "communitiesScreened": len(communities),
        "levelCounts": counts,
        "sources": [
            {"name": "CWFIS active fires (NRCan)", "path": "pipeline/live/active_fires.geojson"},
            {"name": "ECCC real-time hydrometric data", "path": "pipeline/live/river_conditions.geojson"},
            {"name": f"Statistics Canada population estimates ({pop['metadata'].get('year')})",
             "path": "pipeline/population_summary.json"},
        ],
        "hotspots": hotspots[:top],
        "communities": rows[:top],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    return doc


def main():
    ap = argparse.ArgumentParser(description="Rank Canadian communities by screening wildfire and flood scores.")
    ap.add_argument("--min-population", type=int, default=1000)
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()
    doc = build(a.min_population, a.top)
    print(f"Screened {doc['communitiesScreened']} communities; level counts: {doc['levelCounts']}")
    for i, g in enumerate(doc["hotspots"][:10], 1):
        names = ", ".join(c["name"] for c in g["communities"][:4])
        print(f"{i:>2}. {g['hazard']:<8} {g['score']:>3} {g['level']:<8} {g['label']} ({g['detail']}): "
              f"{g['communityCount']} communit{'y' if g['communityCount'] == 1 else 'ies'}, {g['population']:,} people - {names}")
    print(f"Saved {OUT.relative_to(DATA_DIR)}")


if __name__ == "__main__":
    main()
