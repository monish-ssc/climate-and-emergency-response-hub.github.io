"""
flood_pipeline.py - turn ECCC GeoMet water and alert data into a flood risk score per scenario region.

Steps for each region in scenarios.json:
  1. fetch   active real-time stations within RADIUS_KM, their last 24 h of readings, and current alerts
  2. join    each station with its history (monthly means for this month + annual peaks), cached on disk
  3. score   each station: percentile for the month, 24 h trend, status (normal ... flood)
  4. output  GeoJSON map layers + one risk-response per region (contracts/risk-response.schema.json)

Source: ECCC MSC GeoMet OGC API (https://api.weather.gc.ca), free, no API key. Data is provisional.
"""

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

API = "https://api.weather.gc.ca/collections"
DATA_DIR = Path(__file__).resolve().parent
ROOT_DIR = DATA_DIR.parent
OUT_DIR = DATA_DIR / "pipeline"
BASELINE_CACHE = OUT_DIR / "baselines.json"
CONTRACTS_DIR = ROOT_DIR / "api" / "contracts"
RADIUS_KM = 50
MIN_YEARS = 5  # fewer years of history than this -> no percentile / flood peak
FLOOD_MIN_YEARS = 10  # a station needs this many years of monthly history before it can be called "flood"
FLAT_MIN_READINGS = 6  # this many identical recent flow readings = placeholder/estimated value, not a measurement

# Fallback centres for regions without lat/lon in scenarios.json (new regions should set lat/lon there).
REGION_CENTRES = {
    "vancouver": (49.2827, -123.1207),
    "calgary": (51.0447, -114.0719),
    "halifax": (44.6488, -63.5752),
    "clinton": (51.0903, -121.5911),  # wildfire replay region (Pear Lake fire)
    "west-kelowna": (49.8625, -119.5833),  # wildfire replay region (McDougall Creek fire)
}



def region_centre(region):
    """(lat, lon) from scenarios.json, falling back to REGION_CENTRES."""
    if region.get("lat") is not None and region.get("lon") is not None:
        return float(region["lat"]), float(region["lon"])
    return REGION_CENTRES.get(region["id"])


FLOOD_ALERT_WORDS = ("rain", "flood", "storm surge")
ALERT_POINTS = {"yellow": 5, "orange": 10, "red": 20}  # alerts with no colour count 3
STATUS_RANK = ["unknown", "normal", "above_normal", "high", "very_high", "flood"]
DISCLAIMER = (
    "Automated screening from provisional ECCC data; not an official flood forecast."
)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def get_json(url, attempts=4):
    req = urllib.request.Request(
        url, headers={"User-Agent": "climate-hub-hackathon/0.1"}
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
            if e.code < 500 and e.code not in (408, 425, 429):
                raise RuntimeError(detail) from None
            error = detail
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            error = str(e)
        if attempt + 1 == attempts:
            raise RuntimeError(
                f"Request failed after {attempts} attempts: {error}"
            ) from None
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


def fetch(collection, **params):
    """All features matching params (pages until the API runs out, so nothing is silently capped)."""
    feats, offset, page = [], 0, 10000
    while True:
        q = {"f": "json", "limit": page, "offset": offset, **params}
        batch = get_json(f"{API}/{collection}/items?" + urllib.parse.urlencode(q)).get(
            "features", []
        )
        feats += batch
        if len(batch) < page:
            return feats
        offset += page


def to_frame(feats):
    rows = []
    for f in feats:
        row = dict(f.get("properties") or {})
        g = f.get("geometry") or {}
        if g.get("type") == "Point":
            row["_lon"], row["_lat"] = g["coordinates"][:2]
        rows.append(row)
    return pd.DataFrame(rows)


def bbox_around(lat, lon, km):
    dlat, dlon = km / 111.0, km / (111.0 * math.cos(math.radians(lat)))
    return f"{lon - dlon},{lat - dlat},{lon + dlon},{lat + dlat}"


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 6371 * 2 * math.asin(math.sqrt(a))


# ---------- join: historical baseline per station ----------


def fetch_baseline(stn, month):
    # only the columns used below: about half the download, which matters for Canada-wide refreshes
    mm = to_frame(fetch(
        "hydrometric-monthly-mean", STATION_NUMBER=stn, skipGeometry="true",
        properties="DATE,MONTHLY_MEAN_DISCHARGE,MONTHLY_MEAN_LEVEL",
    ))
    pk = to_frame(fetch(
        "hydrometric-annual-peaks", STATION_NUMBER=stn, skipGeometry="true",
        properties="DATA_TYPE_EN,PEAK_CODE_EN,PEAK",
    ))
    out = {"month": month}
    for metric, mean_col, peak_type in (
        ("discharge", "MONTHLY_MEAN_DISCHARGE", "Discharge"),
        ("level", "MONTHLY_MEAN_LEVEL", "Water Level"),
    ):
        hist = (
            mm.loc[mm["DATE"].str.endswith(f"-{month:02d}"), mean_col].dropna().tolist()
            if not mm.empty
            else []
        )
        peaks = (
            pk.loc[
                (pk["DATA_TYPE_EN"] == peak_type) & (pk["PEAK_CODE_EN"] == "Maximum"),
                "PEAK",
            ].dropna()
            if not pk.empty
            else pd.Series(dtype=float)
        )
        out[metric] = {
            "month_means": [float(h) for h in hist],
            # median of yearly maximums ~ a flood expected about every 2 years
            "median_annual_peak": (
                float(peaks.median()) if len(peaks) >= MIN_YEARS else None
            ),
        }
    return out


def load_baselines(stations, month):
    cache = json.load(open(BASELINE_CACHE)) if os.path.exists(BASELINE_CACHE) else {}
    missing = [s for s in stations if cache.get(s, {}).get("month") != month]
    if missing:
        print(f"  fetching history for {len(missing)} stations ...")
        with ThreadPoolExecutor(max_workers=8) as pool:
            for stn, base in zip(
                missing, pool.map(lambda s: fetch_baseline(s, month), missing)
            ):
                cache[stn] = base
        with open(BASELINE_CACHE, "w") as fh:
            json.dump(cache, fh)
    return cache


# ---------- score: one station ----------


def assess_station(readings, base, window_hours=12):
    metric = "discharge" if readings["DISCHARGE"].notna().any() else "level"
    col = metric.upper()
    s = readings.dropna(subset=[col]).sort_values("DATETIME")
    t = pd.to_datetime(s["DATETIME"])
    value, mean_24h = float(s[col].iloc[-1]), float(s[col].mean())
    hist, peak = base[metric]["month_means"], base[metric]["median_annual_peak"]
    if metric == "level":
        # Real-time water levels are often reported against a different gauge datum than the
        # historical record (e.g. 08LF027 Deadman River: 4.27 m live vs yearly maxima of 1.2-2.5 m),
        # so live levels are not compared with history. Level-only stations report their trend only.
        hist, peak = [], None

    # history is monthly means, so rank the 24 h mean (an instant high tide would beat every monthly mean)
    pct = (
        round(100 * sum(h <= mean_24h for h in hist) / len(hist))
        if len(hist) >= MIN_YEARS
        else None
    )
    # trend = last 12 h mean vs the 12 h before; 12 h windows cancel out the ~12.4 h tide cycle
    # (the Canada-wide layer uses shorter windows to keep its download small)
    recent = s.loc[t > t.iloc[-1] - pd.Timedelta(hours=window_hours), col]
    earlier = s.loc[t <= t.iloc[-1] - pd.Timedelta(hours=window_hours), col]
    end, start = float(recent.mean()), (
        float(earlier.mean()) if len(earlier) else float(recent.mean())
    )
    flat = False
    if metric == "discharge":
        change, change_unit = ((end - start) / start * 100 if start else 0.0), "%"
        trend = "rising" if change > 5 else "falling" if change < -5 else "steady"
        # Compare the recent mean, not the last reading: one spike (05CE002 Kneehills Creek: 31.9 m3/s
        # against a 0.75 m3/s mean) must not mark a station "flood".
        ratio = mean_24h / peak if peak else None
        # A perfectly flat flow series is a placeholder or estimate (common at freeze-up), not a
        # measurement: 05CE011 Renwick Creek read exactly 2.05 m3/s for 6 h while the WSC graph showed 0.001.
        flat = len(recent) >= FLAT_MIN_READINGS and recent.nunique() == 1
    else:  # levels use a per-station datum, so compare in metres, never as % or across stations
        change, change_unit = end - start, "m"
        trend = "rising" if change > 0.05 else "falling" if change < -0.05 else "steady"
        ratio = None

    if flat:
        pct, peak, ratio = None, None, None
    if peak is not None and mean_24h >= peak and len(hist) >= FLOOD_MIN_YEARS:
        status = "flood"
    elif (ratio or 0) >= 0.8 or (pct or 0) >= 95:
        status = "very_high"
    elif (pct or 0) >= 90:
        status = "high"
    elif (pct or 0) >= 75:
        status = "above_normal"
    elif pct is None and peak is None:
        status = "unknown"
    else:
        status = "normal"

    return {
        "metric": metric,
        "unit": "m3/s" if metric == "discharge" else "m",
        "value": round(value, 3),
        "mean_24h": round(mean_24h, 3),
        "observed_at": s["DATETIME"].iloc[-1],
        "readings_24h": len(s),
        "change_12h_vs_prev_12h": round(change, 2),
        "change_unit": change_unit,
        "trend": trend,
        "percentile_for_month": pct,
        "history_years": len(hist),
        "median_annual_peak": None if peak is None else round(peak, 3),
        "pct_of_flood_peak": None if ratio is None else round(ratio * 100),
        "status": status,
        "flat_readings": flat,
    }


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


# scores need to be verified against current ECCC guidance and thresholds
def score_region(conds, alerts):
    pcts = [
        c["percentile_for_month"]
        for c in conds
        if c["percentile_for_month"] is not None
    ]
    p = max(pcts, default=None)
    ratios = [
        c["pct_of_flood_peak"] / 100
        for c in conds
        if c["pct_of_flood_peak"] is not None
    ]
    ratios += [1.0 for c in conds if c["status"] == "flood"]
    prox = max(ratios, default=None)
    rising = sum(
        c["trend"] == "rising"
        and STATUS_RANK.index(c["status"]) >= STATUS_RANK.index("above_normal")
        for c in conds
    )
    alert_pts = sum(ALERT_POINTS.get(a.get("risk_colour_en"), 3) for a in alerts)

    factors = [
        factor(
            "river_percentile",
            "Highest river reading vs. history for this month",
            p if p is not None else 0,
            "percentile",
            clamp((p - 50) / 50) * 50 if p is not None else 0,
            (
                "Last-24 h mean ranked against past monthly means for this month."
                if p is not None
                else "No station with enough history."
            ),
        ),
        factor(
            "flood_peak_proximity",
            "Highest flow as share of typical annual peak",
            round(prox * 100) if prox is not None else 0,
            "%",
            clamp((prox - 0.5) / 0.5) * 30 if prox is not None else 0,
            (
                "100% = median yearly peak (~2-year flood)."
                if prox is not None
                else "No peak history."
            ),
        ),
        factor(
            "rising_stations",
            "Above-normal stations still rising",
            rising,
            "stations",
            min(10, 5 * rising),
            "Stations at or above the 75th percentile with a rising 24 h trend.",
        ),
        factor(
            "flood_alerts",
            "Active rain, flood or storm-surge alerts",
            len(alerts),
            "alerts",
            min(10, alert_pts),
            "; ".join(sorted({a["alert_name_en"] for a in alerts}))
            or "None in effect.",
        ),
    ]
    score = round(sum(f["contribution"] for f in factors))
    level = (
        "low"
        if score < 25
        else "moderate" if score < 50 else "high" if score < 75 else "critical"
    )
    return score, level, factors


def summarise(region_label, conds, alerts):
    if not conds:
        return f"No real-time river stations reporting within {RADIUS_KM} km of {region_label}."
    top = max(
        conds,
        key=lambda c: (STATUS_RANK.index(c["status"]), c["percentile_for_month"] or -1),
    )
    pct = (
        f"{top['percentile_for_month']} percentile for the month"
        if top["percentile_for_month"] is not None
        else "no historical percentile"
    )
    return (
        f"{len(conds)} river stations reporting within {RADIUS_KM} km. Highest: {top['station_name']} "
        f"({top['status'].replace('_', ' ')}, {pct}, {top['trend']}). "
        f"{len(alerts)} flood-related weather alert(s) in effect."
    )


def validate(doc):
    try:
        import jsonschema
        import referencing
    except ImportError:
        return "skipped (pip install jsonschema)"
    schemas = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in CONTRACTS_DIR.glob("*.schema.json")
    ]
    registry = referencing.Registry().with_resources(
        (s["$id"], referencing.Resource.from_contents(s)) for s in schemas
    )
    schema = next(s for s in schemas if s["$id"].endswith("/risk-response.schema.json"))
    jsonschema.Draft202012Validator(schema, registry=registry).validate(doc)
    return "valid"


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


def run_region(region, now, since):
    rid, label = region["id"], region["label"]
    lat, lon = region_centre(region)
    bbox = bbox_around(lat, lon, RADIUS_KM)

    # 1. fetch + clean
    st = to_frame(fetch("hydrometric-stations", bbox=bbox))
    if not st.empty:
        st = st[(st["STATUS_EN"] == "Active") & (st["REAL_TIME"] == 1)].copy()
        st["distance_km"] = [
            round(distance_km(lat, lon, a, b), 1)
            for a, b in zip(st["_lat"], st["_lon"])
        ]
        st = st[st["distance_km"] <= RADIUS_KM]
    rt = to_frame(fetch("hydrometric-realtime", bbox=bbox, datetime=f"{iso(since)}/.."))
    reporting = (
        []
        if rt.empty or st.empty
        else sorted(
            set(
                rt.loc[rt[["LEVEL", "DISCHARGE"]].notna().any(axis=1), "STATION_NUMBER"]
            )
            & set(st["STATION_NUMBER"])
        )
    )

    alert_feats = [
        f
        for f in fetch("weather-alerts", bbox=bbox)
        if any(
            w in (f["properties"].get("alert_name_en") or "").lower()
            for w in FLOOD_ALERT_WORDS
        )
        and f["properties"].get("status_en") != "ended"
    ]
    alerts = list(
        {
            (a["alert_name_en"], a["publication_datetime"]): a
            for a in (f["properties"] for f in alert_feats)
        }.values()
    )

    # 2. join with history, 3. score stations
    baselines = load_baselines(reporting, now.month)
    conds = []
    for stn in reporting:
        s = st.loc[st["STATION_NUMBER"] == stn].iloc[0]
        c = assess_station(rt[rt["STATION_NUMBER"] == stn], baselines[stn])
        conds.append(
            {
                "region_id": rid,
                "station_number": stn,
                "station_name": s["STATION_NAME"],
                "province": s["PROV_TERR_STATE_LOC"],
                "distance_km": s["distance_km"],
                **c,
                "_lon": s["_lon"],
                "_lat": s["_lat"],
            }
        )

    station_rows = []
    if not st.empty:
        keep = [
            "STATION_NUMBER",
            "STATION_NAME",
            "PROV_TERR_STATE_LOC",
            "CONTRIBUTOR_EN",
            "DRAINAGE_AREA_GROSS",
            "distance_km",
            "_lon",
            "_lat",
        ]
        for r in json.loads(st[keep].to_json(orient="records")):
            b = baselines.get(r["STATION_NUMBER"], {})
            r.update(
                {
                    "region_id": rid,
                    "reporting_24h": r["STATION_NUMBER"] in reporting,
                    "history_years_discharge": len(
                        b.get("discharge", {}).get("month_means", [])
                    )
                    or None,
                    "median_annual_peak_discharge": b.get("discharge", {}).get(
                        "median_annual_peak"
                    ),
                    "median_annual_peak_level": b.get("level", {}).get(
                        "median_annual_peak"
                    ),
                }
            )
            station_rows.append(r)

    # 4. risk-response contract
    score, level, factors = score_region(conds, alerts)
    retrieved = iso(now)
    latest_obs = max((c["observed_at"] for c in conds), default=retrieved)
    latest_alert = max((a["publication_datetime"] for a in alerts), default=retrieved)
    doc = {
        "contractVersion": "1.0",
        "requestId": str(uuid.uuid4()),
        "region": {"id": rid, "label": label},
        "hazard": "flood",
        "assessment": {
            "score": score,
            "level": level,
            "summary": summarise(label, conds, alerts),
            "factors": factors,
        },
        "sources": [
            {
                "name": "ECCC Real-time hydrometric data",
                "url": f"{API}/hydrometric-realtime",
                "observedAt": latest_obs,
                "retrievedAt": retrieved,
            },
            {
                "name": "ECCC Historical hydrometric data (monthly means, annual peaks)",
                "url": f"{API}/hydrometric-monthly-mean",
                "observedAt": retrieved,
                "retrievedAt": retrieved,
            },
            {
                "name": "ECCC Weather alerts",
                "url": f"{API}/weather-alerts",
                "observedAt": latest_alert,
                "retrievedAt": retrieved,
            },
        ],
        "dataMode": "live",
        "generatedAt": retrieved,
        "expiresAt": iso(now + timedelta(hours=1)),
        "disclaimer": DISCLAIMER,
    }
    alert_rows = [
        {**f["properties"], "region_id": rid, "_geometry": f["geometry"]}
        for f in alert_feats
    ]
    return doc, station_rows, conds, alert_rows


def main():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    since = now - timedelta(hours=24)
    risk_dir = OUT_DIR / "risk"
    risk_dir.mkdir(parents=True, exist_ok=True)
    regions = json.loads((DATA_DIR / "scenarios.json").read_text(encoding="utf-8"))[
        "regions"
    ]

    all_stations, all_conds, all_alerts, failures = [], [], [], []
    for region in regions:
        if region_centre(region) is None:
            print(f"\n{region['id']}: no lat/lon in scenarios.json, skipped")
            continue
        print(f"\n{region['label']} ...")
        try:
            doc, stations, conds, alerts = run_region(region, now, since)
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED: {e}")
            failures.append(region["id"])
            continue
        all_stations += stations
        all_conds += conds
        all_alerts += alerts
        path = risk_dir / f"{region['id']}_flood.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        a = doc["assessment"]
        print(f"  score {a['score']} ({a['level']}) - contract: {validate(doc)}")
        print(f"  {a['summary']}")
        if conds:
            cols = [
                "station_number",
                "station_name",
                "metric",
                "value",
                "mean_24h",
                "change_12h_vs_prev_12h",
                "change_unit",
                "trend",
                "percentile_for_month",
                "pct_of_flood_peak",
                "status",
            ]
            print(
                pd.DataFrame(conds)[cols]
                .sort_values(
                    "status", key=lambda s: s.map(STATUS_RANK.index), ascending=False
                )
                .to_string(index=False)
            )

    write_geojson(OUT_DIR / "stations.geojson", all_stations)
    write_geojson(OUT_DIR / "river_conditions_latest.geojson", all_conds)
    write_geojson(OUT_DIR / "active_alerts.geojson", all_alerts)
    print(
        f"\nSaved: {OUT_DIR}/stations.geojson, river_conditions_latest.geojson, "
        "active_alerts.geojson, risk/*.json"
    )
    if failures:
        raise RuntimeError(f"Flood refresh failed for regions: {', '.join(failures)}")


if __name__ == "__main__":
    main()
