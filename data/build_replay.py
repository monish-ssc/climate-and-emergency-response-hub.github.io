"""
build_replay.py - turn one real wildfire's CWFIS history into a time-stepped replay scenario.

For the demo time slider: each step is the fire as reported at that moment, plus the wildfire
risk-response for the scenario region, scored with exactly the same rules as wildfire_pipeline.py.

Two scenarios are set up and both are built (edit BUILD below to build just one):
  - McDougall Creek wildfire (K52767, Aug 2023) -> region west-kelowna
  - Pear Lake wildfire (C40983, Jul 2026)       -> region clinton

Input:  data/scenarios/<FIRE_ID>/history_raw.geojson (saved by fire_history.py), or, if that file
        is missing, the fire's history is downloaded from CWFIS reported fires.
Output: data/scenarios/<FIRE_ID>/timeline.json        - metadata + one entry per step (fire + risk doc)
        data/scenarios/<FIRE_ID>/timeline_summary.csv - one line per step, for a quick look
        data/scenarios/index.json                     - list of built replays, for a picker in the dashboard

What is real and what is derived (also written into timeline.json "assumptions"):
  - real:     reported size, stage of control and location from CWFIS, at the reported times
  - derived:  sizes between reports are interpolated (steps marked isReported = false);
              size never decreases (small downward revisions are smoothed out);
              the fire's extent is a circle with the reported area, centred on the reported point
  - not replayed: weather alerts (ECCC keeps no archive), other fires active at the same time
"""

import csv
import json
import math
import uuid
from datetime import datetime, timedelta, timezone

import wildfire_pipeline as wp

# Replays to build. Each scenario: its region in scenarios.json, step size, and what actually happened
# according to news reports. Dates and timeLocal are BC local time (PDT, UTC-7); timeLocal is None when the
# source gives no time. Each event was checked against its source on 2026-10-28.
# All are built by default and listed in data/scenarios/index.json so the demo can offer a choice.
BUILD = ["2023_BC_2023-K52767", "2026_BC_2026-C40983"]
HISTORY_COUNT = None   # set a number to skip the CWFIS history query (e.g. when offline)
END_AT_FIRST_OUT = True  # stop the replay at the first "out" (EX) record instead of the last record

SCENARIOS = {
    # McDougall Creek wildfire, August 2023: forced the evacuation of West Kelowna and parts of Kelowna.
    "2023_BC_2023-K52767": {
        "name": "McDougall Creek wildfire",
        "region": "west-kelowna",
        "step_hours": 6,
        "events": [
            {"date": "2023-08-15", "timeLocal": "18:00", "event": "Fire first spotted, less than a hectare",
             "source": "https://www.princegeorgecitizen.com/latest/timeline-of-a-bc-wildfire-as-it-grew-from-spot-fire-to-monster-7485592"},
            {"date": "2023-08-16", "timeLocal": "23:00", "event": "Evacuation alerts for 4,800 properties in West Kelowna and on Westbank First Nation land",
             "source": "https://www.princegeorgecitizen.com/latest/timeline-of-a-bc-wildfire-as-it-grew-from-spot-fire-to-monster-7485592"},
            {"date": "2023-08-17", "timeLocal": None, "event": "First evacuation orders (already in place by a 7 p.m. news conference)",
             "source": "https://www.princegeorgecitizen.com/latest/timeline-of-a-bc-wildfire-as-it-grew-from-spot-fire-to-monster-7485592"},
            {"date": "2023-08-17", "timeLocal": "20:00", "event": "Fire crests the ridge toward Traders Cove and West Kelowna Estates",
             "source": "https://www.princegeorgecitizen.com/latest/timeline-of-a-bc-wildfire-as-it-grew-from-spot-fire-to-monster-7485592"},
            {"date": "2023-08-18", "timeLocal": None, "event": "Homes lost overnight; fire estimated at 10,500 ha",
             "source": "https://globalnews.ca/news/9904497/west-kelowna-fire-evacuation-order-structures-lost"},
            {"date": "2023-08-19", "timeLocal": None, "event": "At least 35,000 people under evacuation orders",
             "source": "https://en.wikipedia.org/wiki/McDougall_Creek_fire"},
            {"date": "2023-09-20", "timeLocal": None, "event": "BC Wildfire Service declares the fire held",
             "source": "https://www.castanetkamloops.net/news/West-Kelowna/535522/Cause-of-destructive-McDougall-Creek-fire-still-under-investigation"},
        ],
    },
    # Pear Lake wildfire, July 2026: forced the evacuation of the Village of Clinton.
    "2026_BC_2026-C40983": {
        "name": "Pear Lake wildfire",
        "region": "clinton",
        "step_hours": 12,
        "events": [
            {"date": "2026-07-20", "timeLocal": None, "event": "SLRD evacuation order for properties in the Pear Lake area",
             "source": "https://cfjctoday.com/?p=1117011"},
            {"date": "2026-07-24", "timeLocal": None, "event": "Entire Village of Clinton evacuated",
             "source": "https://cfjctoday.com/2026/07/25/clinton-fire-department-bcws-working-to-keep-remaining-community-structures-from-burning/"},
            {"date": "2026-07-24", "timeLocal": None,
             "event": "Reception centre for evacuees set up in Kamloops (McArthur Island Sport and Events Centre)",
             "source": "https://www.ctvnews.ca/vancouver/article/entire-bc-village-ordered-to-evacuate-due-to-encroaching-wildfire/"},
            {"date": "2026-08-15", "timeLocal": None, "event": "Clinton evacuation order downgraded to an alert",
             "source": "https://cfjctoday.com/2026/08/25/village-of-clinton-partially-reduces-evacuation-alert-area-in-response-to-pear-lake-wildfire/"},
            {"date": "2026-08-23", "timeLocal": "15:00", "event": "TNRD gives the all clear to nearly 1,243 properties",
             "source": "https://cfjctoday.com/?p=1117011"},
        ],
    },
}
NAMESPACE = uuid.UUID("6f1c2b1e-9a8d-4f43-9c35-2a7d0b6f4e11")  # stable request ids per step


def load_history(FIRE_ID):
    path = wp.DATA_DIR / "scenarios" / FIRE_ID / "history_raw.geojson"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        return data["features"], data.get("source_url") or f"file:{path.name}"
    print(f"{path} not found, downloading history from CWFIS ...")
    field = "national_fire_id" if FIRE_ID.count("_") >= 2 else "agency_fire_id"
    feats, url = wp.cwfis(wp.REPORTED_LAYER, cql=f"{field}='{FIRE_ID}'", sort_by="record_start A")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats, "source_url": url}), encoding="utf-8")
    print(f"  saved {len(feats)} records to {path} (later runs work offline)")
    return feats, url


def reports_from(features):
    """One row per CWFIS record with a usable time and size, oldest first, size made non-decreasing."""
    rows = []
    for f in features:
        c = wp.clean_fire(f)
        t = wp.parse_time((f.get("properties") or {}).get("record_start"))
        if t is None or c["size_ha"] is None or c["_lat"] is None:
            continue
        rows.append({**c, "t": t})
    rows.sort(key=lambda r: r["t"])
    peak = 0.0
    for r in rows:
        r["reported_size_ha"] = r["size_ha"]
        peak = max(peak, r["size_ha"])
        r["size_ha"] = peak
    return rows


def state_at(rows, t):
    """Fire at time t: last report's stage/location, size interpolated to the next report."""
    before = [r for r in rows if r["t"] <= t]
    after = [r for r in rows if r["t"] > t]
    if not before:
        return None
    a = before[-1]
    if after:
        b = after[0]
        frac = (t - a["t"]).total_seconds() / max(1.0, (b["t"] - a["t"]).total_seconds())
        size = a["size_ha"] + frac * (b["size_ha"] - a["size_ha"])
    else:
        size = a["size_ha"]
    return {**a, "size_ha": size, "radius_km": round(wp.radius_km(size), 2), "last_report": a["t"]}


def build(FIRE_ID, built):
    cfg = SCENARIOS[FIRE_ID]
    REGION_ID, STEP_HOURS, REFERENCE_EVENTS = cfg["region"], cfg["step_hours"], cfg["events"]
    region = next((r for r in wp.load_regions() if r["id"] == REGION_ID), None)
    if region is None:
        raise RuntimeError(f"Region '{REGION_ID}' not in scenarios.json")
    lat, lon = wp.region_centre(region)

    features, history_url = load_history(FIRE_ID)
    rows = reports_from(features)
    if len(rows) < 2:
        raise RuntimeError(f"Only {len(rows)} usable records for {FIRE_ID}; pick another fire.")
    start = rows[0]["t"].replace(minute=0, second=0)
    end = rows[-1]["t"]
    if END_AT_FIRST_OUT:
        first_out = next((r["t"] for r in rows if r["stage"] == "EX"), None)
        end = first_out or end
    year = start.year

    if HISTORY_COUNT is not None:
        history, hist_url = HISTORY_COUNT, "manual"
    else:
        try:
            history, hist_url = wp.fire_history_count(lat, lon, year)
        except Exception as e:  # noqa: BLE001
            print(f"Fire history unavailable ({e}); the history factor will be 0.")
            history, hist_url = None, f"{wp.CWFIS_WFS}?typeName={wp.REPORTED_LAYER}"

    steps, t, i = [], start, 0
    while t <= end + timedelta(hours=STEP_HOURS):
        s = state_at(rows, t)
        if s is None:
            t += timedelta(hours=STEP_HOURS)
            continue
        fires = wp.near([s], lat, lon)
        score, level, factors = wp.score_region(fires, [], history)
        for f in factors:
            if f["key"] == "fire_weather_alerts":
                f["explanation"] = "Not replayed: ECCC does not archive past alerts."
        is_reported = any(t - timedelta(hours=STEP_HOURS) < r["t"] <= t for r in rows)
        sources = [
            {
                "name": f"CWFIS Reported fires, history of {s['agency_fire_id']} (NRCan)",
                "url": history_url if history_url.startswith("http") else wp.CWFIS_WFS,
                "observedAt": wp.iso(s["last_report"]),
                "retrievedAt": wp.iso(built),
            },
            {
                "name": "CWFIS Reported fires, history (NRCan)",
                "url": hist_url if str(hist_url).startswith("http") else wp.CWFIS_WFS,
                "observedAt": wp.iso(built),
                "retrievedAt": wp.iso(built),
            },
        ]
        summary = (
            f"Replay {t:%b %d %H:%M} UTC: {s['agency_fire_id']} {s['stage_label']}, about "
            f"{s['size_ha']:,.0f} ha{' (interpolated)' if not is_reported else ''}, "
            + (f"{fires[0]['edge_distance_km']} km from {region['label']} at its approximate edge."
               if fires else f"more than {wp.RADIUS_KM} km from {region['label']}.")
        )
        doc = wp.build_risk_doc(
            region, score, level, factors, summary, sources, "cached", t, valid_hours=STEP_HOURS,
            request_id=str(uuid.uuid5(NAMESPACE, f"{FIRE_ID}:{wp.iso(t)}")),
            disclaimer="Replay of a past fire from agency-reported CWFIS records. Sizes between reports are "
                       "interpolated and the extent is an approximate circle. Not an official fire record.",
        )
        d = wp.distance_km(lat, lon, s["_lat"], s["_lon"])
        steps.append({
            "index": i,
            "t": wp.iso(t),
            "isReported": is_reported,
            "fire": {
                "nationalFireId": s["national_fire_id"],
                "agencyFireId": s["agency_fire_id"],
                "stage": s["stage"],
                "stageLabel": s["stage_label"],
                "severity": s["severity"],
                "sizeHa": round(s["size_ha"], 1),
                "radiusKm": s["radius_km"],
                "distanceKm": round(d, 1),
                "edgeDistanceKm": round(max(0.0, d - s["radius_km"]), 1),
                "lat": s["_lat"],
                "lon": s["_lon"],
                "extentIsApproximate": True,
                "geometry": wp.circle(s["_lat"], s["_lon"], s["radius_km"]),
            },
            "risk": doc,
        })
        i += 1
        t += timedelta(hours=STEP_HOURS)

    # key moments: where the level changes
    moments = [
        {"t": st["t"], "level": st["risk"]["assessment"]["level"], "score": st["risk"]["assessment"]["score"]}
        for k, st in enumerate(steps)
        if k == 0 or st["risk"]["assessment"]["level"] != steps[k - 1]["risk"]["assessment"]["level"]
    ]
    out = {
        "metadata": {
            "scenarioId": f"replay-{FIRE_ID}",
            "label": f"Replay: {cfg['name']} ({rows[0]['agency_fire_id']}, {year})",
            "fireId": FIRE_ID,
            "region": {"id": region["id"], "label": region["label"], "lat": lat, "lon": lon},
            "start": steps[0]["t"],
            "end": steps[-1]["t"],
            "stepHours": STEP_HOURS,
            "stepCount": len(steps),
            "reportCount": len(rows),
            "historyFactorFires": history,
            "dataMode": "cached",
            "builtAt": wp.iso(built),
            "keyMoments": moments,
            "referenceEvents": REFERENCE_EVENTS,
            "assumptions": [
                "Size, stage of control and location are CWFIS agency-reported values at the reported times.",
                f"Sizes between reports are linearly interpolated every {STEP_HOURS} h (isReported = false).",
                "Reported size is made non-decreasing; small downward revisions are smoothed out.",
                "Fire extent is a circle with the reported area around the reported point, not a mapped perimeter.",
                "Weather alerts are not replayed (no ECCC archive); other fires active at the time are not included.",
                "Scores use the same rules as wildfire_pipeline.py.",
            ],
        },
        "steps": steps,
    }
    folder = wp.DATA_DIR / "scenarios" / FIRE_ID
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "timeline.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    with open(folder / "timeline_summary.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["index", "t", "is_reported", "stage", "size_ha", "radius_km", "edge_distance_km", "score", "level"])
        for st in steps:
            f, a = st["fire"], st["risk"]["assessment"]
            w.writerow([st["index"], st["t"], st["isReported"], f["stage"], f["sizeHa"], f["radiusKm"],
                        f["edgeDistanceKm"], a["score"], a["level"]])

    print(f"\n{out['metadata']['label']} -> region {region['label']}")
    print(f"{len(rows)} reports, {len(steps)} steps of {STEP_HOURS} h, {steps[0]['t']} to {steps[-1]['t']}")
    print(f"Fire history factor: {history} past fires\n")
    print("Key moments (risk level changes):")
    for m in moments:
        print(f"  {m['t']}  {m['level']:<9} score {m['score']}")
    print("\nWhat actually happened (news reports):")
    for e in REFERENCE_EVENTS:
        print(f"  {e['date']} {e['timeLocal'] or '     '}  {e['event']}")
    print(f"\nFirst steps' contract check: {wp.validate(steps[0]['risk'])}")
    print(f"Saved: {folder}/timeline.json and timeline_summary.csv")
    m = out["metadata"]
    return {k: m[k] for k in ("scenarioId", "label", "fireId", "region", "start", "end", "stepHours", "stepCount",
                              "keyMoments", "referenceEvents")} | {"file": f"{FIRE_ID}/timeline.json"}


def main():
    built = datetime.now(timezone.utc).replace(microsecond=0)
    entries = []
    for fire_id in BUILD:
        print(f"\n{'=' * 80}\n{SCENARIOS[fire_id]['name']} ({fire_id})\n{'=' * 80}")
        try:
            entries.append(build(fire_id, built))
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED: {e}")
    index = {"builtAt": wp.iso(built), "dataMode": "cached", "replays": entries}
    path = wp.DATA_DIR / "scenarios" / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"\nBuilt {len(entries)} of {len(BUILD)} replays. Index: {path}")


if __name__ == "__main__":
    main()
