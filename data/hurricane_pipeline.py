"""Screen ECCC hurricane tracks against scenario regions and NOAA storm history.

Outputs match the flood pipeline's risk-response contract (hazard: severe_storm).
The score is an unvalidated proximity/alert heuristic, not a hurricane forecast.
"""

import argparse
import json
import math
import shutil
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

if __package__:
    from . import flood_pipeline as flood
else:
    import flood_pipeline as flood

DATA_DIR = Path(__file__).resolve().parent
OUT_DIR = DATA_DIR / "pipeline"
SAMPLES_DIR = DATA_DIR / "samples"
HISTORY_URL = (
    "https://www.ncei.noaa.gov/data/"
    "international-best-track-archive-for-climate-stewardship-ibtracs/"
    "v04r01/access/csv/ibtracs.last3years.list.v04r01.csv"
)
HISTORY_CACHE = DATA_DIR / "raw" / "ibtracs" / "ibtracs.last3years.list.v04r01.csv"
RADIUS_KM = 300
MAX_TRACK_AGE = timedelta(hours=24)
ALERT_WORDS = ("hurricane", "tropical storm", "storm surge")
DISCLAIMER = (
    "Unvalidated prototype screening from provisional ECCC tracks and alerts; "
    "not an official hurricane forecast or a measure of local wind, rain or surge. "
    "Historical NOAA tracks provide context only. Follow official ECCC guidance."
)


def parse_time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"Expected a timezone-aware timestamp: {value}")
    return dt.astimezone(timezone.utc)


def fetch(collection, **params):
    """Follow OGC next links rather than assuming a server's page-size limit."""
    query = urllib.parse.urlencode({"f": "json", "limit": 1000, **params})
    url = f"{flood.API}/{collection}/items?{query}"
    features, seen = [], set()
    while url:
        if url in seen:
            raise RuntimeError(f"Repeated pagination link for {collection}")
        seen.add(url)
        doc = flood.get_json(url)
        if doc.get("type") != "FeatureCollection" or not isinstance(
            doc.get("features"), list
        ):
            raise ValueError(f"{collection}: expected a GeoJSON FeatureCollection")
        features.extend(doc["features"])
        next_url = next(
            (link["href"] for link in doc.get("links", []) if link["rel"] == "next"),
            None,
        )
        if not next_url and doc.get("numberMatched", len(features)) > len(features):
            raise RuntimeError(f"{collection}: incomplete response without a next link")
        url = urllib.parse.urljoin(url, next_url) if next_url else None
    return features


def current_tracks(features, now):
    tracks = []
    for feature in features:
        props = feature["properties"]
        if props.get("active") is not True or props.get("latest_publication") is not True:
            continue
        if props.get("type") != "track":
            continue
        published = parse_time(props["publication_datetime"])
        age = now - published
        if age > MAX_TRACK_AGE or age < -timedelta(minutes=5):
            raise RuntimeError(
                f"Active track {feature.get('id')} has a stale or future publication: "
                f"{props['publication_datetime']}"
            )
        tracks.append(feature)
    return tracks


def bearing(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    delta = math.radians(lon2 - lon1)
    return math.atan2(
        math.sin(delta) * math.cos(b),
        math.cos(a) * math.sin(b) - math.sin(a) * math.cos(b) * math.cos(delta),
    )


def segment_distance_km(lat, lon, start, end):
    """Distance to a great-circle segment, including between forecast vertices."""
    lon1, lat1 = start[:2]
    lon2, lat2 = end[:2]
    length = flood.distance_km(lat1, lon1, lat2, lon2) / 6371
    distance = flood.distance_km(lat1, lon1, lat, lon) / 6371
    angle = bearing(lat1, lon1, lat, lon) - bearing(lat1, lon1, lat2, lon2)
    along = math.atan2(math.sin(distance) * math.cos(angle), math.cos(distance))
    if length > 0 and 0 <= along <= length:
        return 6371 * abs(
            math.asin(max(-1, min(1, math.sin(distance) * math.sin(angle))))
        )
    return min(
        flood.distance_km(lat, lon, lat1, lon1),
        flood.distance_km(lat, lon, lat2, lon2),
    )


def track_distance_km(geometry, lat, lon):
    kind = geometry["type"]
    if kind not in ("LineString", "MultiLineString"):
        raise ValueError(f"Unsupported track geometry: {kind}")
    lines = (
        [geometry["coordinates"]] if kind == "LineString" else geometry["coordinates"]
    )
    if not lines or any(len(line) < 2 for line in lines):
        raise ValueError("A track must contain line segments")
    for line in lines:
        for point in line:
            if (
                len(point) < 2
                or not all(math.isfinite(v) for v in point[:2])
                or not -180 <= point[0] <= 180
                or not -90 <= point[1] <= 90
            ):
                raise ValueError(f"Invalid track coordinate: {point}")
    return min(
        segment_distance_km(lat, lon, start, end)
        for line in lines
        for start, end in zip(line, line[1:])
    )


def load_history(now, path=HISTORY_CACHE, refresh=False):
    path = Path(path)
    if path == HISTORY_CACHE:
        fresh = path.exists() and (
            now - datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            < timedelta(days=7)
        )
        if refresh or not fresh:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".csv.part")
            request = urllib.request.Request(
                HISTORY_URL, headers={"User-Agent": "climate-hub-hackathon/0.1"}
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    with temp.open("wb") as target:
                        shutil.copyfileobj(response, target)
                read_history(temp, now)
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
    return read_history(path, now)


def read_history(path, now):
    # IBTrACS has a units row immediately below its CSV header.
    frame = pd.read_csv(
        path,
        skiprows=[1],
        usecols=["SID", "SEASON", "NAME", "ISO_TIME", "LAT", "LON", "TRACK_TYPE"],
        na_values=[" "],
        skipinitialspace=True,
    )
    for column in ("SEASON", "LAT", "LON"):
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    times = pd.to_datetime(frame["ISO_TIME"], utc=True, errors="raise")
    if frame.empty or times.isna().any() or frame["SID"].isna().any():
        raise ValueError("IBTrACS history is empty or missing storm IDs/timestamps")
    observed = times.max()
    if observed > pd.Timestamp(now) + pd.Timedelta(minutes=5):
        raise ValueError("IBTrACS history contains future observations")
    frame = frame[
        (frame["TRACK_TYPE"].str.strip() == "main")
        & (frame["SEASON"] < now.year)
    ].copy()
    if frame.empty:
        raise ValueError("IBTrACS has no completed-season main tracks")
    # IBTrACS dateline-crossing tracks can use longitudes greater than 180.
    frame["LON"] = (frame["LON"] + 180) % 360 - 180
    valid = frame["LAT"].between(-90, 90) & frame["LON"].between(-180, 180)
    if not valid.all():
        raise ValueError("IBTrACS main tracks contain missing or invalid coordinates")
    return frame, flood.iso(observed.to_pydatetime())


def historical_storms(history, region):
    lat, lon = flood.region_centre(region)
    distances = [
        flood.distance_km(lat, lon, a, b)
        for a, b in zip(history["LAT"], history["LON"])
    ]
    nearby = history.assign(distance_km=distances)
    nearby = nearby[nearby["distance_km"] <= RADIUS_KM]
    closest = nearby.sort_values("distance_km").drop_duplicates("SID")
    return [
        {
            "region_id": region["id"],
            "storm_id": row["SID"],
            "storm_name": row["NAME"],
            "season": int(row["SEASON"]),
            "observed_at": flood.iso(parse_time(row["ISO_TIME"] + "+00:00")),
            "distance_km": round(row["distance_km"], 1),
            "_lat": row["LAT"],
            "_lon": row["LON"],
        }
        for row in closest.to_dict(orient="records")
    ]


def regional_alerts(region, now):
    lat, lon = flood.region_centre(region)
    features = fetch(
        "weather-alerts", bbox=flood.bbox_around(lat, lon, flood.RADIUS_KM)
    )
    active = []
    for feature in features:
        props = feature["properties"]
        if not any(w in (props.get("alert_name_en") or "").lower() for w in ALERT_WORDS):
            continue
        if props.get("status_en") == "ended":
            continue
        if parse_time(props["expiration_datetime"]) <= now:
            continue
        parse_time(props["publication_datetime"])
        active.append(feature)
    return active


def score_region(tracks, alerts, history_count, year_range):
    nearest = min((t["distance_km"] for t in tracks), default=None)
    proximity = 0 if nearest is None else 70 * flood.clamp(1 - nearest / RADIUS_KM)
    # Duplicate alert polygons must not multiply a single bulletin's contribution.
    bulletins = {
        (a["alert_name_en"], a["publication_datetime"]): a for a in alerts
    }.values()
    points = {"warning": 30, "watch": 20, "advisory": 10, "statement": 5}
    alert_points = max(
        (points.get(a["alert_type"], 5) for a in bulletins), default=0
    )
    factors = [
        flood.factor(
            "track_proximity", "Closest active forecast track",
            round(nearest, 1) if nearest is not None else RADIUS_KM,
            "km", proximity,
            "Prototype distance screening; not a wind or storm-surge footprint."
            if nearest is not None else f"No fresh active track within {RADIUS_KM} km.",
        ),
        flood.factor(
            "hurricane_alerts", "Official hurricane-related bulletins",
            len({(a["alert_name_en"], a["publication_datetime"]) for a in alerts}),
            "alerts", alert_points,
            "Highest bulletin: warning 30, watch 20, advisory 10, other 5 points.",
        ),
        flood.factor(
            "historical_storms", "Historical storms with nearby recorded positions",
            history_count, "storms", 0,
            f"Completed seasons {year_range[0]}-{year_range[1]}, within {RADIUS_KM} km; "
            "context only, not a risk contribution or a long-term climatology.",
        ),
    ]
    score = round(sum(f["contribution"] for f in factors))
    level = (
        "low" if score < 25 else "moderate" if score < 50
        else "high" if score < 75 else "critical"
    )
    return score, level, factors


def run_region(region, now, tracks, history, history_observed, history_retrieved):
    lat, lon = flood.region_centre(region)
    nearby = []
    for feature in tracks:
        distance = track_distance_km(feature["geometry"], lat, lon)
        if distance <= RADIUS_KM:
            nearby.append({
                **feature["properties"],
                "region_id": region["id"],
                "distance_km": distance,
                "_geometry": feature["geometry"],
            })
    alert_features = regional_alerts(region, now)
    # GeoJSON retains every affected polygon; scoring deduplicates bulletins.
    alerts = [feature["properties"] for feature in alert_features]
    past = historical_storms(history, region)
    years = (int(history["SEASON"].min()), int(history["SEASON"].max()))
    score, level, factors = score_region(nearby, alerts, len(past), years)
    storm_ids = {
        (t["storm_origin_year"], t["basin"], t["storm_number"]) for t in nearby
    }
    summary = (
        f"{len(storm_ids)} active storm(s) with fresh forecast tracks within "
        f"{RADIUS_KM} km of {region['label']}. "
        f"{factors[1]['value']} hurricane-related bulletin(s) affect the regional search area. "
    )
    if not nearby:
        summary += "No nearby active tracks returned; this does not establish no risk. "
    summary += "This screening does not assess other severe-storm hazards."
    retrieved = flood.iso(now)
    sources = []
    for name, collection, observations in (
        ("ECCC Canadian Hurricane Centre tracks", "hurricanes-track-realtime",
         [t["properties"]["publication_datetime"] for t in tracks]),
        ("ECCC Weather alerts", "weather-alerts",
         [a["publication_datetime"] for a in alerts]),
    ):
        sources.append({
            "name": name, "url": f"{flood.API}/{collection}",
            # An empty collection has no observation: use the query time explicitly.
            "observedAt": max(observations, key=parse_time) if observations else retrieved,
            "retrievedAt": retrieved,
        })
    sources.append({
        "name": "NOAA IBTrACS v04r01 historical storm tracks",
        "url": HISTORY_URL, "observedAt": history_observed,
        "retrievedAt": history_retrieved,
    })
    doc = {
        "contractVersion": "1.0", "requestId": str(uuid.uuid4()),
        "region": {"id": region["id"], "label": region["label"]},
        "hazard": "severe_storm",
        "assessment": {
            "score": score, "level": level, "summary": summary, "factors": factors,
        },
        "sources": sources, "dataMode": "live", "generatedAt": retrieved,
        "expiresAt": flood.iso(now + timedelta(hours=1)), "disclaimer": DISCLAIMER,
    }
    alert_rows = [
        {**f["properties"], "region_id": region["id"], "_geometry": f["geometry"]}
        for f in alert_features
    ]
    return doc, nearby, past, alert_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-history", action="store_true")
    args = parser.parse_args()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    history, observed = load_history(now, HISTORY_CACHE, args.refresh_history)
    retrieved = flood.iso(datetime.fromtimestamp(
        HISTORY_CACHE.stat().st_mtime, timezone.utc
    ))
    tracks = current_tracks(
        fetch("hurricanes-track-realtime", active="true", latest_publication="true"), now
    )
    regions = json.loads((DATA_DIR / "scenarios.json").read_text())["regions"]
    results = []
    for region in regions:
        print(f"{region['label']} ...")
        result = run_region(region, now, tracks, history, observed, retrieved)
        if flood.validate(result[0]) != "valid":
            raise RuntimeError("Risk validation requires jsonschema; install requirements.txt")
        results.append(result)
    # Fetch and validate all regions before overwriting any previous run's outputs.
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for doc, _, _, _ in results:
        path = SAMPLES_DIR / f"{doc['region']['id']}_severe_storm.json"
        path.write_text(json.dumps(doc, indent=2, allow_nan=False), encoding="utf-8")
        print(f"  {doc['region']['label']}: {doc['assessment']['score']} "
              f"({doc['assessment']['level']}) - contract: valid")
    for index, name in (
        (1, "hurricane_tracks_latest.geojson"),
        (2, "hurricane_historical_storms.geojson"),
        (3, "hurricane_active_alerts.geojson"),
    ):
        flood.write_geojson(OUT_DIR / name, [
            row for result in results for row in result[index]
        ])
    print(f"Saved hurricane map layers under {OUT_DIR} and city JSON under {SAMPLES_DIR}")


if __name__ == "__main__":
    main()
