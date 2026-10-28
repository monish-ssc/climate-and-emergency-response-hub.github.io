"""
air_quality_pipeline.py - wildfire smoke / air quality (AQHI) for each scenario region.

Two modes, same output idea as the other pipelines:

  live    (default)  ECCC MSC GeoMet AQHI collections, nearest station to each region in scenarios.json:
                     latest observation + the most recent forecast.
                     -> pipeline/air_quality/<regionId>_aqhi.json, pipeline/aqhi_stations_latest.geojson

  replay  (--replay) BC Ministry of Environment hourly PM2.5 (the AQHI realtime feed has no 2023 history),
                     for the stations nearest each replay in scenarios/index.json, resampled onto the
                     replay's own time steps so the frontend can show smoke next to the fire risk score.
                     -> scenarios/<scenarioFolder>/air_quality.json

Smoke is measured with AQHI+ (PM2.5 only), which BC uses during wildfire smoke events:
AQHI+ = ceil(1-hour PM2.5 in ug/m3 / 10), minimum 1, reported as "10+" above 100 ug/m3.

Sources (free, no API key):
  https://api.weather.gc.ca/collections/aqhi-stations | aqhi-observations-realtime | aqhi-forecasts-realtime
  ftp://ftp.env.gov.bc.ca/pub/outgoing/AIR/  (verified hourly to end of 2023; unverified year-to-date)

Run in Colab from the repo root:
  !python data/air_quality_pipeline.py
  !python data/air_quality_pipeline.py --replay --pm25 2023=PATH_OR_URL --pm25 2026=PATH_OR_URL
The BC PM25 files are big. If the FTP download is slow, download once and pass the local path.
"""

import argparse
import csv
import ftplib
import re
import io
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
OUT_DIR = DATA_DIR / "pipeline"
SCEN_DIR = DATA_DIR / "scenarios"

GEOMET = "https://api.weather.gc.ca/collections"
BC_FTP = "ftp://ftp.env.gov.bc.ca/pub/outgoing/AIR"
BC_STATIONS = f"{BC_FTP}/Air_Monitoring_Stations/bc_air_monitoring_stations.csv"
LIVE_RADIUS_KM = 150     # nearest GeoMet AQHI station must be this close to count
REPLAY_RADIUS_KM = 150   # BC stations used for a replay (Clinton has none of its own)
REPLAY_STATIONS = 3
PST = timezone(timedelta(hours=-8))  # BC files are in Pacific Standard Time all year

CATEGORIES = ((3, "low"), (6, "moderate"), (10, "high"), (math.inf, "very high"))
HEALTH_MESSAGE = {
    "low": "Ideal air quality for outdoor activities.",
    "moderate": "People at risk (heart or lung conditions, older adults, children, pregnant people) "
                "should consider reducing strenuous outdoor activity.",
    "high": "People at risk should reduce or reschedule strenuous outdoor activity; everyone else "
            "should watch for coughing or throat irritation. Consider clean-air shelters.",
    "very high": "People at risk should avoid strenuous outdoor activity; everyone should reduce it. "
                 "Open clean-air spaces and check on vulnerable residents.",
}
DISCLAIMER = ("Station readings may not represent every neighbourhood. AQHI+ uses PM2.5 only. "
              "Replay data from BC is partly unverified. Not an official air quality advisory.")


# ---------- helpers ----------

def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def distance_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def get_bytes(url, timeout=300):
    req = urllib.request.Request(url, headers={"User-Agent": "climate-hub-hackathon/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} for {url}: {e.read().decode('utf-8', 'replace')[:300]}") from None


def get_json(url):
    return json.loads(get_bytes(url, timeout=120).decode("utf-8"))


def geomet(collection, **params):
    feats, offset, page = [], 0, 10000
    while True:
        q = {"f": "json", "limit": page, "offset": offset, **params}
        batch = get_json(f"{GEOMET}/{collection}/items?" + urllib.parse.urlencode(q)).get("features", [])
        feats += batch
        if len(batch) < page:
            return feats
        offset += page


def category(aqhi):
    if aqhi is None:
        return None
    for top, label in CATEGORIES:
        if aqhi <= top:
            return label
    return "very high"


def aqhi_plus(pm25):
    """BC AQHI+ from a 1-hour PM2.5 value (ug/m3)."""
    if pm25 is None or pm25 < 0:
        return None
    return max(1, math.ceil(pm25 / 10))


def display(aqhi):
    if aqhi is None:
        return None
    return "10+" if aqhi > 10 else str(int(round(aqhi)))


def num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def load_regions():
    return json.loads((DATA_DIR / "scenarios.json").read_text())["regions"]


def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2))
    print("wrote", path.relative_to(DATA_DIR))


# ---------- live mode (ECCC GeoMet) ----------

def nearest_station(stations, lat, lon, radius):
    best = None
    for f in stations:
        lon2, lat2 = f["geometry"]["coordinates"][:2]
        d = distance_km(lat, lon, lat2, lon2)
        if d <= radius and (best is None or d < best[0]):
            best = (d, f)
    return best


def latest_observation(location_id):
    feats = geomet("aqhi-observations-realtime", location_id=location_id,
                   sortby="-observation_datetime", limit=1)
    return feats[0]["properties"] if feats else None


def latest_forecast(location_id):
    """Forecast hours from the most recent publication only."""
    feats = geomet("aqhi-forecasts-realtime", location_id=location_id, sortby="-publication_datetime")
    if not feats:
        return []
    newest = feats[0]["properties"].get("publication_datetime")
    rows = [f["properties"] for f in feats if f["properties"].get("publication_datetime") == newest]
    rows.sort(key=lambda p: p.get("forecast_datetime") or "")
    return rows


def live_doc(region, hit, obs, forecast, now):
    d, st = hit if hit else (None, None)
    p = st["properties"] if st else {}
    obs_aqhi = num(obs.get("aqhi")) if obs else None
    fc = [{"t": x.get("forecast_datetime"), "aqhi": num(x.get("aqhi")),
           "display": display(num(x.get("aqhi"))), "category": category(num(x.get("aqhi")))}
          for x in forecast]
    peak = max((x for x in fc if x["aqhi"] is not None), key=lambda x: x["aqhi"], default=None)
    worst = max([v for v in [obs_aqhi, peak and peak["aqhi"]] if v is not None], default=None)
    level = category(worst)
    return {
        "regionId": region["id"],
        "regionLabel": region.get("label", region["id"]),
        "hazard": "air_quality",
        "dataMode": "live" if obs else "unavailable",
        "generatedAt": iso(now),
        "expiresAt": iso(now + timedelta(hours=1)),
        "station": None if not st else {
            "id": p.get("location_id") or st.get("id"),
            "name": p.get("location_name_en"),
            "distanceKm": round(d, 1),
            "lat": st["geometry"]["coordinates"][1],
            "lon": st["geometry"]["coordinates"][0],
        },
        "current": None if not obs else {
            "observedAt": obs.get("observation_datetime"),
            "aqhi": obs_aqhi, "display": display(obs_aqhi), "category": category(obs_aqhi),
        },
        "forecast": fc,
        "forecastPeak": peak,
        "level": level,
        "healthMessage": HEALTH_MESSAGE.get(level),
        "sources": [{"name": "ECCC AQHI (MSC GeoMet)", "url": f"{GEOMET}/aqhi-observations-realtime",
                     "retrievedAt": iso(now)}],
        "disclaimer": DISCLAIMER,
    }


def run_live():
    now = datetime.now(timezone.utc)
    stations = geomet("aqhi-stations")
    print(f"{len(stations)} AQHI stations")
    layer = []
    for region in load_regions():
        hit = nearest_station(stations, region["lat"], region["lon"], LIVE_RADIUS_KM)
        obs, fc = None, []
        if hit:
            loc = hit[1]["properties"].get("location_id") or hit[1]["id"]
            try:
                obs, fc = latest_observation(loc), latest_forecast(loc)
            except RuntimeError as e:
                print(f"  {region['id']}: {e}")
        doc = live_doc(region, hit, obs, fc, now)
        write_json(OUT_DIR / "air_quality" / f"{region['id']}_aqhi.json", doc)
        cur = doc["current"] or {}
        print(f"  {region['id']}: station={doc['station'] and doc['station']['name']} "
              f"aqhi={cur.get('display')} level={doc['level']}")
        if doc["station"]:
            layer.append({"type": "Feature",
                          "geometry": {"type": "Point", "coordinates": [doc["station"]["lon"], doc["station"]["lat"]]},
                          "properties": {"regionId": region["id"], "stationName": doc["station"]["name"],
                                         "aqhi": cur.get("aqhi"), "display": cur.get("display"),
                                         "category": cur.get("category"), "observedAt": cur.get("observedAt")}})
    write_json(OUT_DIR / "aqhi_stations_latest.geojson", {"type": "FeatureCollection", "features": layer})


# ---------- replay mode (BC hourly PM2.5) ----------

def read_bc_stations(src):
    text = (Path(src).read_bytes() if Path(src).exists() else get_bytes(src)).decode("utf-8-sig", "replace")
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {k.strip().upper(): (v or "").strip() for k, v in row.items() if k}
        lat, lon = num(row.get("LAT") or row.get("LATITUDE")), num(row.get("LONG") or row.get("LONGITUDE"))
        ems = row.get("EMS_ID")
        if lat is None or lon is None or not ems:
            continue
        out.append({"ems_id": ems, "name": row.get("STATION_NAME") or row.get("STATION_NAME_FULL") or ems,
                    "lat": lat, "lon": abs(lon) * -1})  # some BC files store longitude as positive west
    return out


def parse_pst(s):
    """BC timestamps, e.g. '2023-08-17 13:00' or '2023-08-17 24:00' (= next day 00:00), in PST."""
    s = (s or "").strip()
    if not s:
        return None
    extra = timedelta(0)
    if " 24:" in s:
        s, extra = s.replace(" 24:", " 00:"), timedelta(days=1)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return (datetime.strptime(s, fmt) + extra).replace(tzinfo=PST)
        except ValueError:
            continue
    return None


def read_pm25(src, ems_ids, start, end):
    """Hourly PM2.5 for the given stations and window: {ems_id: [(utc_time, ug_m3), ...]}.
    Streams the file so the multi-GB verified CSV never has to fit in memory."""
    if Path(src).exists():
        fh = open(src, "r", encoding="utf-8-sig", errors="replace", newline="")
    else:
        print(f"  downloading {src} (can take a while)...")
        req = urllib.request.Request(src, headers={"User-Agent": "climate-hub-hackathon/0.1"})
        fh = io.TextIOWrapper(urllib.request.urlopen(req, timeout=600), encoding="utf-8-sig",
                              errors="replace", newline="")
    out = {e: [] for e in ems_ids}
    with fh:
        reader = csv.DictReader(fh)
        reader.fieldnames = [c.strip().upper() for c in reader.fieldnames]
        for row in reader:
            ems = (row.get("EMS_ID") or "").strip()
            if ems not in out:
                continue
            t = parse_pst(row.get("DATE_PST") or row.get("DATE"))
            if t is None or not (start <= t <= end):
                continue
            v = num(row.get("RAW_VALUE")) if row.get("RAW_VALUE") not in (None, "") else num(row.get("ROUNDED_VALUE"))
            if v is not None and v >= 0:
                out[ems].append((t.astimezone(timezone.utc), v))
    for e in out:
        out[e].sort()
    return out


def step_values(series, steps, step_hours):
    """Max hourly PM2.5 in the window (t - step_hours, t] for each step time."""
    res, i = [], 0
    for t in steps:
        lo = t - timedelta(hours=step_hours)
        vals = [v for (tt, v) in series if lo < tt <= t]
        res.append(max(vals) if vals else None)
    return res


def replay_doc(scen, stations_used, pm, steps, step_hours, source_url):
    rows = []
    for k, t in enumerate(steps):
        per = []
        for st in stations_used:
            v = pm[st["ems_id"]][k]
            per.append({"emsId": st["ems_id"], "pm25Max": v, "aqhiPlus": aqhi_plus(v)})
        nearest = next((p for p in per if p["pm25Max"] is not None), None)  # stations sorted by distance
        a = nearest["aqhiPlus"] if nearest else None
        rows.append({"index": k, "t": iso(t), "pm25Max": nearest and nearest["pm25Max"], "aqhiPlus": a,
                     "display": display(a), "category": category(a), "stations": per})
    valid = [r for r in rows if r["aqhiPlus"] is not None]
    peak = max(valid, key=lambda r: r["aqhiPlus"], default=None)
    first_high = next((r for r in valid if r["aqhiPlus"] >= 7), None)
    return {
        "scenarioId": scen["scenarioId"],
        "fireId": scen.get("fireId"),
        "region": scen.get("region"),
        "hazard": "air_quality",
        "metric": "AQHI+ (PM2.5), max hourly value in each replay step",
        "stepHours": step_hours,
        "stations": [{k: st[k] for k in ("ems_id", "name", "lat", "lon", "distanceKm")} for st in stations_used],
        "summary": {
            "stepsWithData": len(valid), "steps": len(rows),
            "peak": peak and {"t": peak["t"], "aqhiPlus": peak["aqhiPlus"], "display": peak["display"],
                              "pm25Max": peak["pm25Max"]},
            "firstHighOrWorse": first_high and first_high["t"],
        },
        "steps": rows,
        "sources": [{"name": "BC Ministry of Environment hourly PM2.5", "url": source_url}],
        "disclaimer": DISCLAIMER,
    }


def ftp_find_pm25(year, max_depth=3):
    """Search the BC FTP tree for hourly PM2.5 CSVs and pick the best one for `year`.
    Folder names on the server change from year to year, so look instead of guessing."""
    host, root = "ftp.env.gov.bc.ca", "/pub/outgoing/AIR"
    found = []

    def walk(ftp, path, depth):
        try:
            names = ftp.nlst(path)
        except ftplib.all_errors:
            return
        for n in names:
            full = n if n.startswith("/") else f"{path}/{n}"
            base = full.rstrip("/").rsplit("/", 1)[-1]
            if base.lower().endswith(".csv"):
                if re.search(r"pm_?2[._]?5", base, re.I):
                    found.append(full)
            elif "." not in base and depth < max_depth and base.lower() not in ("meteorological", "met"):
                walk(ftp, full, depth + 1)

    with ftplib.FTP(host, timeout=120) as ftp:
        ftp.login()
        for sub in ("AnnualSummary", "Hourly_Raw_Air_Data"):
            walk(ftp, f"{root}/{sub}", 1)
    print(f"  PM2.5 files on the FTP server: {found or 'none found'}")

    def rank(path):
        p = path.lower()
        years = [int(y) for y in re.findall(r"(?<!\d)(19\d\d|20\d\d)(?!\d)", p)]
        if str(year) in p and len(set(years)) == 1:
            return 0                                  # a file or folder for exactly this year
        if len(years) >= 2 and min(years) <= year <= max(years):
            return 1                                  # a range such as 2009-2023
        if "latest" in p and year <= datetime.now().year - 1:
            return 2
        if "year_to_date" in p and year == datetime.now().year:
            return 0
        return 9

    best = sorted(found, key=rank)
    if not best or rank(best[0]) == 9:
        raise RuntimeError(f"No PM2.5 file for {year} found. Download one by hand and pass --pm25 {year}=PATH")
    return f"ftp://{host}{best[0]}"


def pick_source(year, overrides):
    if str(year) in overrides:
        return overrides[str(year)]
    src = ftp_find_pm25(year)
    print(f"  using {src} for {year}")
    return src


def run_replay(overrides, stations_src):
    index = json.loads((SCEN_DIR / "index.json").read_text())
    stations = read_bc_stations(stations_src)
    print(f"{len(stations)} BC air monitoring stations")
    for scen in index.get("replays", []):
        reg = scen["region"]
        start, end = parse_time(scen["start"]), parse_time(scen["end"])
        step_hours = scen.get("stepHours", 6)
        steps, t = [], start
        while t <= end:
            steps.append(t)
            t += timedelta(hours=step_hours)
        near = sorted(({**s, "distanceKm": round(distance_km(reg["lat"], reg["lon"], s["lat"], s["lon"]), 1)}
                       for s in stations), key=lambda s: s["distanceKm"])
        near = [s for s in near if s["distanceKm"] <= REPLAY_RADIUS_KM]
        seen, uniq = set(), []
        for s in near:  # the BC list repeats some stations (one row per instrument / EMS ID)
            if s["ems_id"] in seen or s["name"] in {u["name"] for u in uniq}:
                seen.add(s["ems_id"])
                continue
            seen.add(s["ems_id"])
            uniq.append(s)
        near = uniq
        src = pick_source(start.year, overrides)
        pm = read_pm25(src, [s["ems_id"] for s in near], start - timedelta(hours=step_hours), end)
        # keep the closest stations that actually reported during the replay
        used = [s for s in near if pm[s["ems_id"]]][:REPLAY_STATIONS]
        if not used:
            print(f"  {scen['scenarioId']}: no PM2.5 data within {REPLAY_RADIUS_KM} km - check the source file")
            continue
        sv = {s["ems_id"]: step_values(pm[s["ems_id"]], steps, step_hours) for s in used}
        doc = replay_doc(scen, used, sv, steps, step_hours, src)
        folder = scen.get("fireId") or scen["scenarioId"].replace("replay-", "")
        write_json(SCEN_DIR / folder / "air_quality.json", doc)
        s = doc["summary"]
        print(f"  {scen['scenarioId']}: stations={[u['name'] for u in used]} peak={s['peak']} "
              f"firstHigh={s['firstHighOrWorse']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replay", action="store_true", help="build air_quality.json for the wildfire replays")
    ap.add_argument("--pm25", action="append", default=[], metavar="YEAR=PATH_OR_URL",
                    help="PM2.5 CSV to use for replays starting in YEAR (repeatable)")
    ap.add_argument("--stations", default=BC_STATIONS, help="BC station list CSV (path or URL)")
    a = ap.parse_args()
    if a.replay:
        run_replay(dict(x.split("=", 1) for x in a.pm25), a.stations)
    else:
        run_live()


if __name__ == "__main__":
    main()
