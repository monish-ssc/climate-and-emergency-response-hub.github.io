"""
build_reception_centres.py - evacuee reception centres for the wildfire replays (resource planning layer).

Input  data/resources/reception_centres.csv   hand-built from EmergencyInfoBC, City of West Kelowna and
                                              local news; every row has a source_url.
Steps  1. geocode rows with no lat/lon (OpenStreetMap Nominatim, 1 request/second, cached back to the CSV)
       2. distance from the replay region and from the fire's position when the centre opened
       3. nearest hospitals from data/healthcare/healthcare_facilities_clean.csv
       4. which replay step the centre opens at, so the replay can show it appearing on the map
Output data/resources/reception_centres.geojson
       data/scenarios/<scenario_folder>/resources.json

Run in Colab from the repo root:   !python data/build_reception_centres.py
Rows that fail to geocode are printed: fill lat/lon by hand in the CSV (right-click in Google Maps) and rerun.
"""

import csv
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
CSV_PATH = DATA_DIR / "resources" / "reception_centres.csv"
HEALTH_CSV = DATA_DIR / "healthcare" / "healthcare_facilities_clean.csv"
SCEN_DIR = DATA_DIR / "scenarios"
PDT = timezone(timedelta(hours=-7))   # all rows are BC summer local time
NOMINATIM = "https://nominatim.openstreetmap.org/search"
HOSPITALS_PER_CENTRE = 3
HEALTH_URL = ("https://raw.githubusercontent.com/monish-ssc/climate-and-emergency-response-hub.github.io/"
              "data-exploration/data/healthcare/healthcare_facilities_clean.csv")
ROLE_LABEL = {"ess_reception_centre": "ESS reception centre", "overnight_group_lodging": "Overnight group lodging"}


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_local(s):
    """'2023-08-17' or '2023-08-17T23:00' (BC local time) -> aware datetime, or None."""
    s = (s or "").strip()
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=PDT)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def geocode(row):
    queries = [f"{row['address']}, {row['city']}, British Columbia, Canada",
               f"{row['name']}, {row['city']}, British Columbia, Canada"]
    for q in queries:
        url = NOMINATIM + "?" + urllib.parse.urlencode({"q": q, "format": "json", "limit": 1, "countrycodes": "ca"})
        req = urllib.request.Request(url, headers={"User-Agent": "climate-hub-hackathon/0.1 (student project)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            hits = json.loads(r.read().decode("utf-8"))
        time.sleep(1.1)  # Nominatim usage policy: max 1 request per second
        if hits:
            return float(hits[0]["lat"]), float(hits[0]["lon"]), q
    return None


def load_rows():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows, list(rows[0].keys())


def fill_coordinates(rows, fields):
    changed, missing = False, []
    for r in rows:
        if num(r["lat"]) is not None and num(r["lon"]) is not None:
            continue
        try:
            hit = geocode(r)
        except Exception as e:  # network problems shouldn't stop the rest
            print(f"  geocode error for {r['name']}: {e}")
            hit = None
        if hit:
            r["lat"], r["lon"] = f"{hit[0]:.5f}", f"{hit[1]:.5f}"
            changed = True
            print(f"  geocoded {r['name']} -> {r['lat']}, {r['lon']}  (check on a map)")
        else:
            missing.append(r["name"])
    if changed:
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
    for m in missing:
        print(f"  NO COORDINATES for {m}: add lat/lon to the CSV by hand")


def load_hospitals():
    if not HEALTH_CSV.exists():
        print(f"  {HEALTH_CSV} not found; downloading it from the repo")
        try:
            HEALTH_CSV.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(HEALTH_URL, headers={"User-Agent": "climate-hub-hackathon/0.1"})
            with urllib.request.urlopen(req, timeout=120) as r:
                HEALTH_CSV.write_bytes(r.read())
        except Exception as e:
            print(f"  download failed ({e}); skipping nearest hospitals (pass --healthcare PATH)")
            return []
    out = []
    with open(HEALTH_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            lat, lon = num(r.get("latitude")), num(r.get("longitude"))
            if lat is None or lon is None or (r.get("odhf_facility_type") or "") != "Hospitals":
                continue
            out.append({"name": r["facility_name"].title(), "city": (r.get("city") or "").title(),
                        "type": r.get("source_facility_type"), "lat": lat, "lon": lon})
    return out


def nearest_hospitals(hospitals, lat, lon):
    ranked = sorted(hospitals, key=lambda h: distance_km(lat, lon, h["lat"], h["lon"]))
    picked = []
    for h in ranked:
        # the source lists some hospitals twice under slightly different names; skip anything within 1 km
        if any(distance_km(h["lat"], h["lon"], p["lat"], p["lon"]) < 1 for p in picked):
            continue
        picked.append({**h, "distanceKm": round(distance_km(lat, lon, h["lat"], h["lon"]), 1)})
        if len(picked) == HOSPITALS_PER_CENTRE:
            break
    return picked


def load_replay(folder):
    p = SCEN_DIR / folder / "timeline.json"
    if not p.exists():
        return None, []
    tl = json.loads(p.read_text())
    steps = [(datetime.fromisoformat(s["t"].replace("Z", "+00:00")), s) for s in tl["steps"]]
    return tl.get("metadata", {}), steps


def step_at(steps, when):
    """First replay step at or after `when` (None if outside the replay)."""
    if not when:
        return None
    return next(((t, s) for t, s in steps if t >= when), None)


def main():
    global HEALTH_CSV
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--healthcare", default=str(HEALTH_CSV), help="path to healthcare_facilities_clean.csv")
    HEALTH_CSV = Path(ap.parse_args().healthcare)
    rows, fields = load_rows()
    fill_coordinates(rows, fields)
    hospitals = load_hospitals()
    print(f"{len(hospitals)} hospitals with coordinates")
    index = {r["scenarioId"].replace("replay-", ""): r
             for r in json.loads((SCEN_DIR / "index.json").read_text()).get("replays", [])}

    features, by_scenario = [], {}
    for r in rows:
        lat, lon = num(r["lat"]), num(r["lon"])
        if lat is None or lon is None:
            continue
        folder = r["scenario_folder"]
        opened, closed = parse_local(r["opened_local"]), parse_local(r["closed_local"])
        region = index.get(folder, {}).get("region", {})
        _, steps = load_replay(folder)
        hit = step_at(steps, opened)
        fire = hit[1].get("fire") if hit else None
        rec = {
            "name": r["name"], "address": r["address"], "city": r["city"],
            "role": r["role"], "roleLabel": ROLE_LABEL.get(r["role"], r["role"]),
            "openedAt": iso(opened), "closedAt": iso(closed),
            "opensAtStep": hit[1]["index"] if hit else (0 if not opened and steps else None),
            "pets": r["pets"] or None, "notes": r["notes"] or None,
            "lat": lat, "lon": lon,
            "distanceFromRegionKm": round(distance_km(lat, lon, region["lat"], region["lon"]), 1) if region else None,
            "distanceFromFireKm": (round(distance_km(lat, lon, fire["lat"], fire["lon"]), 1)
                                   if fire and fire.get("lat") is not None else None),
            "nearestHospitals": nearest_hospitals(hospitals, lat, lon) if hospitals else [],
            "source": r["source_url"],
        }
        by_scenario.setdefault(folder, []).append(rec)
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                         "properties": {"scenarioFolder": folder, "regionId": r["region_id"],
                                        **{k: v for k, v in rec.items() if k not in ("lat", "lon")}}})

    out = DATA_DIR / "resources" / "reception_centres.geojson"
    out.write_text(json.dumps({"type": "FeatureCollection", "features": features}, indent=2))
    print("wrote", out.relative_to(DATA_DIR))

    for folder, recs in by_scenario.items():
        recs.sort(key=lambda x: x["opensAtStep"] if x["opensAtStep"] is not None else 10 ** 6)
        doc = {
            "scenarioFolder": folder,
            "region": index.get(folder, {}).get("region"),
            "generatedAt": iso(datetime.now(timezone.utc)),
            "receptionCentres": recs,
            "note": ("Hand-compiled from public announcements; capacity was not published. Opening times "
                     "given only as a date use that day's first replay step."),
        }
        p = SCEN_DIR / folder / "resources.json"
        p.write_text(json.dumps(doc, indent=2))
        print("wrote", p.relative_to(DATA_DIR), f"({len(recs)} centres)")


if __name__ == "__main__":
    main()
