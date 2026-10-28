"""
explore_water_weather.py - quick look at ECCC weather alerts and water level data.

Source: ECCC MSC GeoMet OGC API (https://api.weather.gc.ca), free, no API key.

For each dataset it:
  - downloads a sample (saved as raw JSON in data/raw/geomet/)
  - shows the size, every field with its type, % missing and number of distinct values
  - prints the first rows and the top values of short text fields
  - saves a CSV in data/explore/ to open in Excel or Sheets

Edit DATASETS and BBOX below to change what it fetches.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

API = "https://api.weather.gc.ca/collections"
DATA_DIR = Path(__file__).resolve().parent

# Area to fetch: (min lon, min lat, max lon, max lat). None = all of Canada.
# British Columbia: (-139, 48, -114, 60)   Manitoba: (-102, 49, -88.9, 60)
BBOX = (-139, 48, -114, 60)

LAST_24H = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ") + "/.."

# name -> (collection id, extra query parameters, max rows)
DATASETS = {
    "weather_alerts":      ("weather-alerts",       {}, 500),                     # current warnings/watches/advisories
    "hydro_stations":      ("hydrometric-stations", {}, 2000),                    # station list with locations
    "hydro_realtime":      ("hydrometric-realtime", {"datetime": LAST_24H}, 2000),  # water level + flow, last 24 h
}

RAW_DIR, OUT_DIR = DATA_DIR / "raw" / "geomet", DATA_DIR / "explore"
pd.set_option("display.max_columns", 50, "display.width", 200, "display.max_colwidth", 40)


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "climate-hub-hackathon/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None


def fetch(collection, params, max_rows):
    """Download up to max_rows features, page by page (the API pages with offset)."""
    feats, offset, page = [], 0, min(500, max_rows)
    while len(feats) < max_rows:
        q = {"f": "json", "limit": page, "offset": offset, **params}
        if BBOX:
            q["bbox"] = ",".join(map(str, BBOX))
        url = f"{API}/{collection}/items?" + urllib.parse.urlencode(q)
        batch = get_json(url).get("features", [])
        feats += batch
        if len(batch) < page:
            break
        offset += page
    return feats[:max_rows], url


def to_frame(feats):
    rows = []
    for f in feats:
        row = dict(f.get("properties") or {})
        g = f.get("geometry") or {}
        if g.get("type") == "Point":
            row["_lon"], row["_lat"] = g["coordinates"][:2]
        row["_geometry_type"] = g.get("type")
        rows.append(row)
    return pd.DataFrame(rows)


def explore(name, df):
    print(f"\n{'=' * 80}\n{name}: {len(df)} rows x {df.shape[1]} columns\n{'=' * 80}")
    if df.empty:
        print("No rows. Try BBOX = None, or a different time window.")
        return
    summary = pd.DataFrame({
        "type": df.dtypes.astype(str),
        "missing_%": (df.isna().mean() * 100).round(1),
        "distinct": df.nunique(dropna=True),
        "example": df.apply(lambda c: c.dropna().astype(str).iloc[0][:40] if c.notna().any() else ""),
    })
    print("\nFields:\n", summary.to_string())
    print("\nFirst 5 rows:\n", df.head().to_string())
    for col in df.columns:
        is_text = df[col].dtype == object or pd.api.types.is_string_dtype(df[col])
        if is_text and 1 < df[col].nunique() <= 15:
            print(f"\nTop values of {col}:\n", df[col].value_counts(dropna=False).head(10).to_string())
    num = df.select_dtypes("number")
    if not num.empty:
        print("\nNumeric columns:\n", num.describe().round(2).to_string())


RAW_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

for name, (collection, params, max_rows) in DATASETS.items():
    print(f"\nDownloading {name} ({collection}) ...")
    try:
        feats, url = fetch(collection, params, max_rows)
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED: {e}")
        continue
    raw_path = RAW_DIR / f"{name}_{stamp}.json"
    csv_path = OUT_DIR / f"{name}_{stamp}.csv"
    with open(raw_path, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "source_url": url, "features": feats}, fh)
    df = to_frame(feats)
    df.to_csv(csv_path, index=False)
    explore(name, df)
    print(f"\nSaved: {raw_path} and {csv_path}")
