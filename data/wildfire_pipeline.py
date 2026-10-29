"""
wildfire_pipeline.py - turn CWFIS wildfire data and ECCC alerts into a wildfire risk score per scenario region.

Same shape as flood_pipeline.py. Steps for each region in scenarios.json:
  1. fetch   active fires in Canada (CWFIS, filtered to "now"), fire-related weather alerts near the region,
             and large past fires near the region (CWFIS reported fires, previous 10 years)
  2. clean   -1 = unknown, drop stale records, approximate each fire's extent as a circle of the same area
  3. score   proximity of out-of-control fires, how many, how much area is burning, alerts, fire history
  4. output  GeoJSON map layers + one risk-response per region (contracts/risk-response.schema.json)

Sources: CWFIS GeoServer (NRCan, https://geoserver.cwfif.nrcan.gc.ca) and ECCC MSC GeoMet
(https://api.weather.gc.ca). Both free, no API key. Fire data is agency-reported and may lag.

build_replay.py imports score_region / build_risk_doc from this file, so replayed scores use the same rules.
"""

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
ROOT_DIR = DATA_DIR.parent
OUT_DIR = DATA_DIR / "pipeline"
CONTRACTS_DIR = ROOT_DIR / "api" / "contracts"

CWFIS_WFS = "https://geoserver.cwfif.nrcan.gc.ca/geoserver/wfs"
ACTIVE_LAYER = "public:cwfif_national_activefires"
REPORTED_LAYER = "public:cwfif_national_reportedfires"
GEOMET = "https://api.weather.gc.ca/collections"

RADIUS_KM = 150          # wildfires matter at a wider range than rivers (smoke, evacuation routes)
STALE_DAYS = 60          # active-fire records with no status update for this long are treated as stale
HISTORY_YEARS = 10       # look-back for the fire history factor
HISTORY_MIN_HA = 1000    # only count past fires at least this big

# Used only when a region in scenarios.json has no "lat"/"lon".
REGION_CENTRES = {
    "vancouver": (49.2827, -123.1207),
    "calgary": (51.0447, -114.0719),
    "halifax": (44.6488, -63.5752),
    "clinton": (51.0903, -121.5911),
    "west-kelowna": (49.8625, -119.5833),
}

# Stage of control (CWFIS codes): weight of a fire's threat, map severity 1-4, label.
STAGE = {
    "OC": (1.0, 4, "out of control"),
    "BH": (0.6, 3, "being held"),
    "UC": (0.25, 2, "under control"),
    "EX": (0.0, 1, "out"),
}
FIRE_ALERT_WORDS = ("heat", "air quality", "smoke", "fire", "wind")
ALERT_POINTS = {"yellow": 3, "orange": 6, "red": 10}  # alerts with no colour count 2
LEVELS = ((25, "low"), (50, "moderate"), (75, "high"), (101, "critical"))
DISCLAIMER = (
    "Automated screening from agency-reported CWFIS data and ECCC alerts; fire extents are "
    "approximate circles from reported size. Not an official fire behaviour forecast."
)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def get_json(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "climate-hub-hackathon/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None


def cwfis(layer, cql=None, sort_by=None, count=10000):
    """GetFeature as GeoJSON in lon/lat. No startIndex: paging makes CWFIS return HTTP 400."""
    q = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeName": layer,
        "outputFormat": "application/json", "srsName": "EPSG:4326", "count": str(count),
    }
    if sort_by:
        q["sortBy"] = sort_by
    if cql:
        q["CQL_FILTER"] = cql
    url = CWFIS_WFS + "?" + urllib.parse.urlencode(q)
    data = get_json(url)
    matched = data.get("numberMatched")
    if isinstance(matched, int) and matched > len(data.get("features", [])):
        print(f"  note: {matched} records match, {len(data['features'])} returned (CWFIS cap)")
    return data.get("features", []), url


def geomet(collection, **params):
    """All features matching params (pages until the API runs out)."""
    feats, offset, page = [], 0, 10000
    while True:
        q = {"f": "json", "limit": page, "offset": offset, **params}
        batch = get_json(f"{GEOMET}/{collection}/items?" + urllib.parse.urlencode(q)).get("features", [])
        feats += batch
        if len(batch) < page:
            return feats
        offset += page


def bbox_around(lat, lon, km):
    dlat, dlon = km / 111.0, km / (111.0 * math.cos(math.radians(lat)))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 6371 * 2 * math.asin(math.sqrt(a))


def num(v):
    """CWFIS uses -1 for unknown."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f >= 0 else None


def parse_time(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def radius_km(size_ha):
    """Radius of a circle with the fire's reported area."""
    return math.sqrt((size_ha or 0) * 10000 / math.pi) / 1000


def circle(lat, lon, r_km, n=48):
    """Polygon approximating a circle (for map display of approximate fire extent)."""
    r_km = max(r_km, 0.05)
    pts = []
    for i in range(n + 1):
        a = 2 * math.pi * i / n
        dlat = (r_km / 111.0) * math.cos(a)
        dlon = (r_km / (111.0 * math.cos(math.radians(lat)))) * math.sin(a)
        pts.append([round(lon + dlon, 5), round(lat + dlat, 5)])
    return {"type": "Polygon", "coordinates": [pts]}


# ---------- clean: one CWFIS record -> fire dict ----------


def clean_fire(feature):
    p = feature.get("properties") or {}
    g = feature.get("geometry") or {}
    if g.get("type") == "Point":
        lon, lat = g["coordinates"][:2]
    else:  # no geometry: use the attribute columns (coordinates can be negative, so not num())
        try:
            lon, lat = float(p.get("longitude")), float(p.get("latitude"))
        except (TypeError, ValueError):
            lon, lat = None, None
    stage = str(p.get("stage_of_control_status") or "").upper()
    size = num(p.get("fire_size"))
    return {
        "national_fire_id": p.get("national_fire_id"),
        "agency_fire_id": p.get("agency_fire_id"),
        "agency": p.get("agency_code"),
        "stage": stage,
        "stage_label": STAGE.get(stage, (0, 2, stage.lower() or "unknown"))[2],
        "severity": STAGE.get(stage, (0, 2, ""))[1],
        "size_ha": size,
        "radius_km": round(radius_km(size), 2),
        "cause": p.get("national_fire_cause"),
        "response": p.get("response_type"),
        "status_date": p.get("status_date"),
        "situation_report_date": p.get("situation_report_date"),
        "fire_year": p.get("fire_year"),
        "_lat": lat,
        "_lon": lon,
    }


def latest_per_fire(fires):
    """Keep the newest record per fire (by status_date)."""
    best = {}
    for f in fires:
        k = f["national_fire_id"] or f"{f['_lat']},{f['_lon']}"
        if k not in best or (f["status_date"] or "") > (best[k]["status_date"] or ""):
            best[k] = f
    return list(best.values())


def near(fires, lat, lon, radius=RADIUS_KM):
    out = []
    for f in fires:
        if f["_lat"] is None or f["_lon"] is None:
            continue
        d = distance_km(lat, lon, f["_lat"], f["_lon"])
        edge = max(0.0, d - f["radius_km"])
        if edge <= radius:
            out.append({**f, "distance_km": round(d, 1), "edge_distance_km": round(edge, 1)})
    return sorted(out, key=lambda f: f["edge_distance_km"])


# ---------- score: one region -> risk-response contract ----------


def clamp(x):
    return max(0.0, min(1.0, x))


def factor(key, label, value, unit, contribution, explanation):
    return {
        "key": key,
        "label": label,
        "value": value,
        "unit": unit,
        "contribution": round(contribution, 1),
        "explanation": explanation,
    }


def size_text(size_ha):
    return f"{size_ha:,.0f} ha" if size_ha is not None else "size not reported"


def size_weight(size_ha):
    """0.3 for a spot fire, rising to 1.0 at 10,000 ha (log scale): big fires spread farther, faster."""
    return clamp(0.3 + 0.7 * math.log10(1 + (size_ha or 0)) / 4)


def fire_threat(f, radius=RADIUS_KM):
    """0-1: stage of control x closeness of the approximate edge x size."""
    return STAGE.get(f["stage"], (0.0,))[0] * clamp(1 - f["edge_distance_km"] / radius) * size_weight(f["size_ha"])


IMMINENT_KM = 5  # an uncontained fire edge this close to the region centre adds the "imminent threat" factor


# weights are a transparent prototype, to be reviewed with fire-management guidance:
# proximity 40 + imminent threat 20 + fire count 10 + burning area 10 + alerts 10 + history 10 = 100
def score_region(fires, alerts, history_count, radius=RADIUS_KM):
    """fires: output of near(); alerts: GeoMet alert properties; history_count: int or None."""
    threat = [(fire_threat(f, radius), f) for f in fires]
    best_w, best_f = max(threat, key=lambda t: t[0], default=(0.0, None))
    imminent = [
        (STAGE[f["stage"]][0] * clamp(1 - f["edge_distance_km"] / IMMINENT_KM), f)
        for f in fires if f["stage"] in ("OC", "BH") and f["edge_distance_km"] < IMMINENT_KM
    ]
    imm_w, imm_f = max(imminent, key=lambda t: t[0], default=(0.0, None))
    oc = [f for f in fires if f["stage"] == "OC"]
    bh = [f for f in fires if f["stage"] == "BH"]
    burning_ha = sum(f["size_ha"] or 0 for f in oc + bh)
    alert_pts = sum(ALERT_POINTS.get(a.get("risk_colour_en"), 2) for a in alerts)

    factors = [
        factor(
            "fire_proximity",
            "Most threatening fire: distance to its approximate edge",
            best_f["edge_distance_km"] if best_f else radius,
            "km",
            best_w * 40,
            (
                f"{best_f['agency_fire_id']} ({best_f['stage_label']}, {size_text(best_f['size_ha'])}) "
                f"is {best_f['edge_distance_km']} km away at its approximate edge. Weighted by stage of "
                f"control, closeness and size."
                if best_f
                else f"No active fires within {radius} km."
            ),
        ),
        factor(
            "imminent_threat",
            f"Uncontained fire edge within {IMMINENT_KM} km",
            imm_f["edge_distance_km"] if imm_f else IMMINENT_KM,
            "km",
            imm_w * 20,
            (
                f"{imm_f['agency_fire_id']} ({imm_f['stage_label']}) is within {IMMINENT_KM} km at its approximate "
                f"edge; full points when the edge reaches the community."
                if imm_f
                else f"No uncontained fire within {IMMINENT_KM} km."
            ),
        ),
        factor(
            "out_of_control_fires",
            f"Uncontained fires within {radius} km",
            len(oc) + len(bh),
            "fires",
            min(10, 5 * len(oc) + 2.5 * len(bh)),
            f"{len(oc)} out of control, {len(bh)} being held (5 and 2.5 points each, max 10).",
        ),
        factor(
            "active_burned_area",
            f"Area of uncontained fires within {radius} km",
            round(burning_ha),
            "ha",
            clamp(math.log10(1 + burning_ha) / 5) * 10,
            "Reported size of out-of-control and being-held fires; 100,000 ha scores the maximum.",
        ),
        factor(
            "fire_weather_alerts",
            "Active heat, smoke, air quality or wind alerts",
            len(alerts),
            "alerts",
            min(10, alert_pts),
            "; ".join(sorted({a.get("alert_name_en") or "alert" for a in alerts})) or "None in effect.",
        ),
        factor(
            "fire_history",
            f"Past fires over {HISTORY_MIN_HA:,} ha within {radius} km, last {HISTORY_YEARS} years",
            history_count if history_count is not None else 0,
            "fires",
            clamp(math.log10(1 + (history_count or 0)) / 2) * 10,
            (
                "From CWFIS reported fires; log scale, about 6 points for 15 fires, 10 points at 99 or more."
                if history_count is not None
                else "History unavailable (CWFIS query failed)."
            ),
        ),
    ]
    score = min(100, round(sum(f["contribution"] for f in factors)))
    level = next(name for limit, name in LEVELS if score < limit)
    return score, level, factors


def summarise(region_label, fires, alerts, radius=RADIUS_KM):
    if not fires:
        return f"No active wildfires reported within {radius} km of {region_label}. {len(alerts)} fire-weather alert(s) in effect."
    oc = sum(f["stage"] == "OC" for f in fires)
    top = fires[0]
    return (
        f"{len(fires)} active fire(s) within {radius} km, {oc} out of control. Closest: {top['agency_fire_id']} "
        f"({top['stage_label']}, {size_text(top['size_ha'])}, about {top['edge_distance_km']} km to its edge). "
        f"{len(alerts)} fire-weather alert(s) in effect."
    )


def build_risk_doc(region, score, level, factors, summary, sources, data_mode, generated_at, valid_hours=1,
                   request_id=None, disclaimer=DISCLAIMER):
    return {
        "contractVersion": "1.0",
        "requestId": request_id or str(uuid.uuid4()),
        "region": {"id": region["id"], "label": region["label"]},
        "hazard": "wildfire",
        "assessment": {"score": score, "level": level, "summary": summary, "factors": factors},
        "sources": sources,
        "dataMode": data_mode,
        "generatedAt": iso(generated_at),
        "expiresAt": iso(generated_at + timedelta(hours=valid_hours)),
        "disclaimer": disclaimer,
    }


def validate(doc):
    try:
        import jsonschema
        import referencing
    except ImportError:
        return "skipped (pip install jsonschema)"
    schemas = [json.loads(p.read_text(encoding="utf-8")) for p in CONTRACTS_DIR.glob("*.schema.json")]
    if not schemas:
        return f"skipped (no contracts in {CONTRACTS_DIR})"
    registry = referencing.Registry().with_resources(
        (s["$id"], referencing.Resource.from_contents(s)) for s in schemas
    )
    schema = next(s for s in schemas if s["$id"].endswith("/risk-response.schema.json"))
    jsonschema.Draft202012Validator(schema, registry=registry).validate(doc)
    return "valid"


# ---------- fetch helpers used by main() and build_replay.py ----------


def region_centre(region):
    if "lat" in region and "lon" in region:
        return float(region["lat"]), float(region["lon"])
    return REGION_CENTRES.get(region["id"])


def fire_history_count(lat, lon, before_year, radius=RADIUS_KM):
    """Distinct past fires >= HISTORY_MIN_HA within radius, in the HISTORY_YEARS before before_year."""
    w, s, e, n = bbox_around(lat, lon, radius)
    cql = (
        f"fire_year>={before_year - HISTORY_YEARS} AND fire_year<{before_year} "
        f"AND fire_size>={HISTORY_MIN_HA} AND BBOX(geometry,{w},{s},{e},{n},'EPSG:4326')"
    )
    feats, url = cwfis(REPORTED_LAYER, cql=cql, sort_by="record_start D")
    ids = set()
    for f in feats:
        c = clean_fire(f)
        if c["_lat"] is not None and distance_km(lat, lon, c["_lat"], c["_lon"]) <= radius:
            ids.add(c["national_fire_id"])
    return len(ids), url


def fire_alerts(lat, lon, radius=RADIUS_KM):
    w, s, e, n = bbox_around(lat, lon, radius)
    feats = [
        f
        for f in geomet("weather-alerts", bbox=f"{w},{s},{e},{n}")
        if any(word in (f["properties"].get("alert_name_en") or "").lower() for word in FIRE_ALERT_WORDS)
        and f["properties"].get("status_en") != "ended"
    ]
    unique = {
        (f["properties"].get("alert_name_en"), f["properties"].get("publication_datetime")): f for f in feats
    }
    return list(unique.values())


def load_regions():
    return json.loads((DATA_DIR / "scenarios.json").read_text(encoding="utf-8"))["regions"]


# ---------- outputs ----------


def write_geojson(path, records):
    feats = []
    for r in records:
        r = dict(r)
        lon, lat = r.pop("_lon", None), r.pop("_lat", None)
        geom = r.pop("_geometry", None) or {"type": "Point", "coordinates": [lon, lat]}
        feats.append({"type": "Feature", "geometry": geom, "properties": r})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh)


def run_region(region, all_fires, active_url, now):
    lat, lon = region_centre(region)
    fires = near(all_fires, lat, lon)

    try:
        alert_feats = fire_alerts(lat, lon)
    except Exception as e:  # noqa: BLE001
        print(f"  alerts unavailable: {e}")
        alert_feats = []
    alerts = [f["properties"] for f in alert_feats]

    try:
        history, history_url = fire_history_count(lat, lon, now.year)
    except Exception as e:  # noqa: BLE001
        print(f"  fire history unavailable: {e}")
        history, history_url = None, f"{CWFIS_WFS}?typeName={REPORTED_LAYER}"

    score, level, factors = score_region(fires, alerts, history)
    retrieved = iso(now)
    latest_fire = max((f["status_date"] for f in fires if f["status_date"]), default=retrieved)
    latest_alert = max((a.get("publication_datetime") or "" for a in alerts), default="") or retrieved
    sources = [
        {"name": "CWFIS Active wildfires (NRCan)", "url": active_url, "observedAt": latest_fire, "retrievedAt": retrieved},
        {"name": "CWFIS Reported fires, history (NRCan)", "url": history_url, "observedAt": retrieved, "retrievedAt": retrieved},
        {"name": "ECCC Weather alerts", "url": f"{GEOMET}/weather-alerts", "observedAt": latest_alert, "retrievedAt": retrieved},
    ]
    doc = build_risk_doc(region, score, level, factors, summarise(region["label"], fires, alerts), sources, "live", now)

    fire_rows = [{**f, "region_id": region["id"]} for f in fires]
    extent_rows = [
        {
            "region_id": region["id"],
            "national_fire_id": f["national_fire_id"],
            "agency_fire_id": f["agency_fire_id"],
            "stage": f["stage"],
            "severity": f["severity"],
            "size_ha": f["size_ha"],
            "radius_km": f["radius_km"],
            "extent_is_approximate": True,
            "_geometry": circle(f["_lat"], f["_lon"], f["radius_km"]),
        }
        for f in fires
        if f["size_ha"]
    ]
    alert_rows = [{**f["properties"], "region_id": region["id"], "_geometry": f["geometry"]} for f in alert_feats]
    return doc, fire_rows, extent_rows, alert_rows


def fetch_active_fires(now):
    """All of Canada in one request (~150 fires, small), cleaned, deduplicated, stale records dropped."""
    feats, url = cwfis(
        ACTIVE_LAYER,
        cql="now()>=record_start AND now()<=record_end",
        sort_by="agency_code A,record_start D",
    )
    fires = latest_per_fire([clean_fire(f) for f in feats])
    cutoff = iso(now - timedelta(days=STALE_DAYS))
    fresh = [f for f in fires if (f["status_date"] or "") >= cutoff and f["stage"] != "EX"]
    print(f"Active fires: {len(feats)} records, {len(fires)} fires, {len(fresh)} after dropping "
          f"out/stale (> {STALE_DAYS} days without update)")
    return fresh, url


def main():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    risk_dir = OUT_DIR / "risk"
    risk_dir.mkdir(parents=True, exist_ok=True)

    fires, active_url = fetch_active_fires(now)
    all_fires, all_extents, all_alerts = [], [], []
    for region in load_regions():
        if not region_centre(region):
            print(f"\n{region['id']}: no centre (add lat/lon in scenarios.json), skipped")
            continue
        print(f"\n{region['label']} ...")
        try:
            doc, fire_rows, extents, alerts = run_region(region, fires, active_url, now)
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED: {e}")
            continue
        all_fires += fire_rows
        all_extents += extents
        all_alerts += alerts
        with open(risk_dir / f"{region['id']}_wildfire.json", "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        a = doc["assessment"]
        print(f"  score {a['score']} ({a['level']}) - contract: {validate(doc)}")
        print(f"  {a['summary']}")
        for f in a["factors"]:
            print(f"    {f['contribution']:>5}  {f['label']}: {f['value']} {f['unit']}")

    write_geojson(OUT_DIR / "active_fires.geojson", all_fires)
    write_geojson(OUT_DIR / "active_fire_extents.geojson", all_extents)
    write_geojson(OUT_DIR / "fire_weather_alerts.geojson", all_alerts)
    print(f"\nSaved: {OUT_DIR}/active_fires.geojson, active_fire_extents.geojson, "
          f"fire_weather_alerts.geojson, risk/*_wildfire.json")


if __name__ == "__main__":
    main()
