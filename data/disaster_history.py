"""
disaster_history.py - historical disaster context per region and hazard, from the Canadian Disaster Database.

Input:  data/raw/cdd/cdd-extract.xlsx (or .csv): the CDD extract from Public Safety Canada / Open Government.
        Without it, the script rebuilds from data/history/cdd_events_clean.csv (written by an earlier run).
Output: data/history/disaster_history.json  - per region and hazard: past disaster counts, impact statistics,
                                              an evacuation baseline for resource planning, and the most
                                              relevant past events (for AI briefings and the dashboard)
        data/history/cdd_events_clean.csv   - every CDD event with standard column names, hazard, provinces,
                                              coordinates and location precision

Hazards follow the API contract: wildfire (CDD type WF), flood (FL), severe_storm (HU hurricane/tropical storm,
SS storm surge). Regions come from scenarios.json; each region's province is read from its label ("..., BC").

Matching, and why:
  - Province: every CDD event lists the provinces it affected, and that field is reliable. Province counts
    and impact statistics use it.
  - Distance: CDD coordinates are often placeholders for a whole area (one point is shared by 38 "Prairie
    Provinces" events), so distance is used only for events whose place is specific (one province, not a
    province name or a broad area) and only to rank "similar events" nearby.

The CDD only holds events that meet its criteria (for example 10+ deaths or 100+ people affected), ends at
the extract's last event, and recent costs may be preliminary. These caveats are written into the output.

Needs pandas (and openpyxl for .xlsx; both in requirements.txt).
"""

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent
INPUTS = [DATA_DIR / "raw" / "cdd" / "cdd-extract.xlsx", DATA_DIR / "raw" / "cdd" / "cdd-extract.csv"]
OUT_DIR = DATA_DIR / "history"
SOURCE = {
    "name": "Canadian Disaster Database (Public Safety Canada)",
    "url": "https://www.publicsafety.gc.ca/cnt/rsrcs/cndn-dsstr-dtbs/index-en.aspx",
}

HAZARD_CODES = {"WF": "wildfire", "FL": "flood", "HU": "severe_storm", "SS": "severe_storm"}
NEARBY_KM = {"wildfire": 150, "flood": 150, "severe_storm": 300}  # same ranges as the live pipelines
RECENT_YEARS = 30          # "recent" window for counts and the evacuation baseline
SIMILAR_EVENTS = 5
PROVINCE_NAMES = {
    "BC": "british columbia", "AB": "alberta", "SK": "saskatchewan", "MB": "manitoba", "ON": "ontario",
    "QC": "quebec", "NB": "new brunswick", "NS": "nova scotia", "PE": "prince edward island",
    "NL": "newfoundland and labrador", "YT": "yukon", "NT": "northwest territories", "NU": "nunavut",
}
BROAD_WORDS = ("provinces", "canada", "maritime", "prairie", "atlantic", "southern", "northern", "eastern",
               "western", "central", "interior", "coast", "region")
CAVEATS = [
    "The CDD only lists disasters that meet its criteria (for example 10+ deaths or 100+ people affected); "
    "smaller events are not counted.",
    "Coverage ends at the extract's last event date (see metadata.coverage).",
    "Event locations are often approximate; province matching is used for counts, distance only for "
    "events with a specific place.",
    "Costs are in the dollars of the event year and may be preliminary for recent events.",
    "Evacuation figures are reported totals for the whole event, which may cover several communities.",
]


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(a))


def load_raw():
    for path in INPUTS:
        if path.exists():
            df = pd.read_excel(path) if path.suffix == ".xlsx" else pd.read_csv(path, encoding="utf-8-sig")
            return df, path
    raise SystemExit(f"CDD extract not found. Put it at {INPUTS[0]} (or .csv).")


def point_of(wkt):
    """Point coordinates, or the average vertex of a polygon (good enough for an approximate location)."""
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", str(wkt))]
    if len(nums) < 2:
        return None, None
    lons, lats = nums[0::2], nums[1::2]
    return sum(lats) / len(lats), sum(lons) / len(lons)


def clean(raw):
    raw = raw.dropna(subset=["EVENT_ID"]).copy()
    prov_col = next(c for c in raw.columns if c.upper().startswith("PROVINCES_AFFECTED"))
    df = pd.DataFrame({
        "event_id": raw["EVENT_ID"].astype(int),
        "event_type": raw["EVENT_TYPE"],
        "event_type_name": raw["EVENT_TYPE_DESCRIPTION"],
        "start_date": pd.to_datetime(raw["EVENT_START_DATE"], errors="coerce"),
        "end_date": pd.to_datetime(raw["EVENT_END_DATE"], errors="coerce"),
        "place": raw["PLACE"].astype(str).str.strip(),
        "comment": raw["COMMENT"].astype(str).str.strip(),
        "evacuated": pd.to_numeric(raw["EVACUATED"], errors="coerce"),
        "dead": pd.to_numeric(raw["DEAD"], errors="coerce"),
        "injured": pd.to_numeric(raw["INJURED"], errors="coerce"),
        "total_cost": pd.to_numeric(raw["TOTAL_COST"], errors="coerce"),
        "provinces": raw[prov_col].fillna("").astype(str).str.upper().str.split(),
    })
    df["hazard"] = df["event_type"].map(HAZARD_CODES)
    pts = raw["GEOG_OBJ"].map(point_of)
    df["lat"] = [p[0] for p in pts]
    df["lon"] = [p[1] for p in pts]
    df["year"] = df["start_date"].dt.year
    df["location_precision"] = [precision(p, provs) for p, provs in zip(df["place"], df["provinces"])]
    return df


def precision(place, provinces):
    low = place.lower().strip(" .")
    if len(provinces) != 1:
        return "broad"
    if low in PROVINCE_NAMES.values() or low in {"bc", "b.c."}:
        return "broad"
    if any(w in low for w in BROAD_WORDS):
        return "broad"
    return "local"


def num(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else (int(v) if float(v).is_integer() else round(float(v), 2))


def impact_stats(g):
    ev = g["evacuated"][g["evacuated"] > 0]
    cost = g["total_cost"][g["total_cost"] > 0]
    return {
        "events": int(len(g)),
        "eventsWithEvacuations": int(len(ev)),
        "evacuatedMedian": num(ev.median()) if len(ev) else None,
        "evacuatedP75": num(ev.quantile(0.75)) if len(ev) else None,
        "evacuatedMax": num(ev.max()) if len(ev) else None,
        "evacuatedTotal": num(ev.sum()) if len(ev) else 0,
        "deathsTotal": num(g["dead"].fillna(0).sum()),
        "eventsWithCost": int(len(cost)),
        "costMedian": num(cost.median()) if len(cost) else None,
    }


def event_record(r, km=None):
    return {
        "eventId": int(r.event_id),
        "startDate": r.start_date.strftime("%Y-%m-%d") if pd.notna(r.start_date) else None,
        "type": r.event_type_name,
        "place": r.place,
        "provinces": r.provinces,
        "evacuated": num(r.evacuated),
        "dead": num(r.dead),
        "totalCost": num(r.total_cost),
        "distanceKm": None if km is None else round(km, 1),
        "locationPrecision": r.location_precision,
        "summary": (r.comment[:280] + "...") if len(r.comment) > 280 else r.comment,
    }


def region_province(region):
    m = re.search(r",\s*([A-Z]{2})\s*$", region.get("label", ""))
    return region.get("province") or (m.group(1) if m else None)


def region_hazard(df, region, hazard, last_year):
    prov = region_province(region)
    lat, lon = float(region["lat"]), float(region["lon"])
    g = df[(df["hazard"] == hazard) & df["provinces"].map(lambda p: prov in p)].copy()
    recent = g[g["year"] > last_year - RECENT_YEARS]

    g["km"] = [
        distance_km(lat, lon, a, b) if p == "local" and pd.notna(a) else None
        for a, b, p in zip(g["lat"], g["lon"], g["location_precision"])
    ]
    nearby = g[g["km"].notna() & (g["km"] <= NEARBY_KM[hazard])]

    # similar events: nearby specific places first (most evacuated, then most recent), then the province's biggest
    ranked = pd.concat([
        nearby.sort_values(["evacuated", "start_date"], ascending=False, na_position="last"),
        g[~g.index.isin(nearby.index)].sort_values(["evacuated", "start_date"], ascending=False, na_position="last"),
    ]).head(SIMILAR_EVENTS)

    base = recent if (recent["evacuated"] > 0).sum() >= 3 else g
    ev = base["evacuated"][base["evacuated"] > 0]
    decades = g.groupby((g["year"] // 10 * 10).astype("Int64")).size()
    return {
        "province": prov,
        "provinceEvents": int(len(g)),
        f"provinceEventsLast{RECENT_YEARS}Years": int(len(recent)),
        "eventsPerDecadeRecent": round(len(recent) / (RECENT_YEARS / 10), 1),
        "eventsByDecade": {str(k): int(v) for k, v in decades.items()},
        "nearbyEvents": int(len(nearby)),
        "nearbyRadiusKm": NEARBY_KM[hazard],
        "impact": impact_stats(g),
        "evacuationBaseline": {
            "median": num(ev.median()) if len(ev) else None,
            "p75": num(ev.quantile(0.75)) if len(ev) else None,
            "basedOnEvents": int(len(ev)),
            "window": f"last {RECENT_YEARS} years" if base is recent else "all years",
            "note": "Reported people evacuated per past disaster of this type in the province. Use as a "
                    "planning reference, not a forecast.",
        },
        "similarEvents": [event_record(r, None if pd.isna(r.km) else r.km) for r in ranked.itertuples()],
    }


def load_clean_csv():
    """The cleaned CSV this script wrote last time; lets it run without the raw CDD extract."""
    path = OUT_DIR / "cdd_events_clean.csv"
    df = pd.read_csv(path, encoding="utf-8")
    df["start_date"] = pd.to_datetime(df["start_date"], errors="coerce")
    df["end_date"] = pd.to_datetime(df["end_date"], errors="coerce")
    df["provinces"] = df["provinces"].fillna("").astype(str).str.split()
    return df, path


def main():
    if any(p.exists() for p in INPUTS):
        raw, path = load_raw()
        df = clean(raw)
    elif (OUT_DIR / "cdd_events_clean.csv").exists():
        print("Raw CDD extract not found; rebuilding from history/cdd_events_clean.csv")
        df, path = load_clean_csv()
    else:
        load_raw()  # exits with the "not found" message
    built = datetime.now(timezone.utc).replace(microsecond=0)
    first, last = df["start_date"].min(), df["start_date"].max()
    regions = json.loads((DATA_DIR / "scenarios.json").read_text(encoding="utf-8"))["regions"]
    hazards = sorted(set(HAZARD_CODES.values()))
    ours = df[df["hazard"].notna()]

    out = {
        "metadata": {
            "source": SOURCE,
            "file": path.name,
            "builtAt": iso(built),
            "coverage": {"firstEvent": first.strftime("%Y-%m-%d"), "lastEvent": last.strftime("%Y-%m-%d"),
                         "events": int(len(df)), "eventsForOurHazards": int(len(ours))},
            "hazardTypes": {"wildfire": ["WF Wildfire"], "flood": ["FL Flood"],
                            "severe_storm": ["HU Hurricane / Typhoon / Tropical Storm", "SS Storm Surge"]},
            "recentWindowYears": RECENT_YEARS,
            "caveats": CAVEATS,
        },
        "national": {h: impact_stats(ours[ours["hazard"] == h]) for h in hazards},
        "regions": {},
    }
    for region in regions:
        if not region_province(region) or "lat" not in region:
            print(f"{region['id']}: no province or lat/lon in scenarios.json, skipped")
            continue
        out["regions"][region["id"]] = {
            "label": region["label"],
            "hazards": {h: region_hazard(df, region, h, last.year) for h in hazards},
        }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "disaster_history.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    csv = df.assign(provinces=df["provinces"].map(" ".join))
    csv.to_csv(OUT_DIR / "cdd_events_clean.csv", index=False)

    print(f"CDD: {len(df)} events, {first:%Y-%m-%d} to {last:%Y-%m-%d}; our hazards: "
          f"{ours['hazard'].value_counts().to_dict()}")
    for rid, r in out["regions"].items():
        print(f"\n{r['label']}")
        for h, x in r["hazards"].items():
            b = x["evacuationBaseline"]
            top = x["similarEvents"][0] if x["similarEvents"] else None
            print(f"  {h:<12} {x['province']}: {x['provinceEvents']:>3} events ({x['eventsPerDecadeRecent']}/decade recently), "
                  f"{x['nearbyEvents']} nearby; evac median {b['median']} (n={b['basedOnEvents']}, {b['window']})"
                  + (f"; top: {top['startDate']} {top['place']} ({top['evacuated']} evacuated)" if top else ""))
    print(f"\nSaved {OUT_DIR / 'disaster_history.json'} and {OUT_DIR / 'cdd_events_clean.csv'}")


if __name__ == "__main__":
    main()
