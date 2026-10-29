import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

# --------------------------------------------------
# Configuration
# --------------------------------------------------

DATASET_ID = "543fe07a-fd79-40e9-a829-ccd697526765"
API_URL = "https://open.canada.ca/data/api/3/action/package_show"

DATA_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = DATA_DIR / "healthcare"

# Optional: provide a GeoJSON file containing hazard polygons.
# Hazard boundaries need to be provided as a GeoJSON file containing polygons.
HAZARD_FILE = DATA_DIR / "hazard_zones.geojson"

# Candidate search radius for alternative facilities (km).
ALTERNATIVE_RADIUS_KM = 50


# --------------------------------------------------
# Helpers
# --------------------------------------------------


def normalize_column(name):
    name = str(name).strip().lower()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    return name.strip("_")


def read_csv(content):
    try:
        return pd.read_csv(io.BytesIO(content), low_memory=False, encoding="utf-8-sig")
    except UnicodeDecodeError:
        print("CSV is not UTF-8; reading with Windows-1252 encoding.")
        return pd.read_csv(io.BytesIO(content), low_memory=False, encoding="cp1252")


def load_resource(url):
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    content = response.content

    if zipfile.is_zipfile(io.BytesIO(content)):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = [
                f
                for f in archive.namelist()
                if f.lower().endswith((".csv", ".xlsx", ".xls"))
                and not f.startswith("__MACOSX/")
            ]
            if not files:
                raise ValueError("No CSV or Excel file found in ZIP.")

            # Prefer English-named files when possible.
            english = [
                f
                for f in files
                if "english" in f.lower() or "_en" in f.lower() or "/en" in f.lower()
            ]
            filename = english[0] if english else files[0]

            with archive.open(filename) as file:
                if filename.lower().endswith(".csv"):
                    return read_csv(file.read())
                return pd.read_excel(file)

    if url.lower().split("?")[0].endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(content))

    return read_csv(content)


def find_column(columns, candidates):
    """Find a column using common naming variations."""
    for candidate in candidates:
        if candidate in columns:
            return candidate

    for col in columns:
        if any(candidate in col for candidate in candidates):
            return col

    return None


def haversine_km(lat1, lon1, lat2, lon2):
    """Approximate great-circle distance between coordinates."""
    import numpy as np

    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 6371 * 2 * np.arcsin(np.sqrt(a))


# --------------------------------------------------
# 1. Download and ingest
# --------------------------------------------------


def ingest_data():
    response = requests.get(
        API_URL,
        params={"id": DATASET_ID},
        timeout=30,
    )
    response.raise_for_status()
    metadata = response.json()

    if not metadata.get("success"):
        raise RuntimeError("Could not retrieve dataset metadata.")

    dataset = metadata["result"]
    resources = dataset.get("resources", [])

    supported = {"csv", "zip", "xlsx", "xls"}
    candidates = [
        r
        for r in resources
        if r.get("url")
        and (
            str(r.get("format", "")).lower() in supported
            or r["url"]
            .lower()
            .split("?")[0]
            .endswith((".csv", ".zip", ".xlsx", ".xls"))
        )
    ]

    if not candidates:
        raise RuntimeError("No downloadable CSV, ZIP, or Excel resource.")

    english = [
        r
        for r in candidates
        if "english" in str(r.get("name", "")).lower()
        or "_en" in str(r.get("name", "")).lower()
    ]
    resource = english[0] if english else candidates[0]

    print("Dataset:", dataset.get("title"))
    print("Resource:", resource.get("name"))

    df = load_resource(resource["url"])
    df.columns = [normalize_column(c) for c in df.columns]
    df = df.dropna(how="all").dropna(axis=1, how="all")

    for col in df.select_dtypes(include=["object", "string"]).columns:
        df[col] = df[col].map(lambda x: x.strip() if isinstance(x, str) else x)

    df.to_csv(OUTPUT_DIR / "healthcare_facilities_clean.csv", index=False)
    df.attrs["source"] = {
        "datasetId": DATASET_ID,
        "url": f"https://open.canada.ca/data/en/dataset/{DATASET_ID}",
        "resourceUrl": resource["url"],
        "retrievedAt": datetime.now(timezone.utc).isoformat(),
        "modifiedAt": dataset.get("metadata_modified"),
    }
    return df


# --------------------------------------------------
# 2. Detect important columns
# --------------------------------------------------


def detect_columns(df):
    cols = df.columns.tolist()

    mapping = {
        "name": find_column(cols, ["facility_name", "official_name", "name"]),
        "type": find_column(
            cols, ["odhf_facility_type", "facility_type", "type_of_facility", "type"]
        ),
        "latitude": find_column(cols, ["latitude", "lat"]),
        "longitude": find_column(cols, ["longitude", "lon", "lng"]),
        "province": find_column(cols, ["province", "territory", "pr_uid"]),
        "municipality": find_column(
            cols, ["municipality", "city", "census_subdivision"]
        ),
        "address": find_column(
            cols,
            ["source_format_str_address", "full_address", "address", "street_name"],
        ),
    }

    print("\nDetected columns:")
    for key, value in mapping.items():
        print(f"  {key}: {value}")

    return mapping


# --------------------------------------------------
# 3. Data quality report
# --------------------------------------------------


def analyze_quality(df):
    report = pd.DataFrame(
        {
            "data_type": df.dtypes.astype(str),
            "missing_count": df.isna().sum(),
            "missing_percent": (df.isna().mean() * 100).round(2),
            "unique_values": df.nunique(dropna=True),
        }
    )

    report.to_csv(OUTPUT_DIR / "data_quality_report.csv")
    print("\nData quality report saved.")
    print("Rows:", len(df), "| Columns:", len(df.columns))


# --------------------------------------------------
# 4. Facility type and regional summaries
# --------------------------------------------------


def analyze_facilities(df, columns):
    type_col = columns["type"]
    province_col = columns["province"]
    municipality_col = columns["municipality"]

    if type_col:
        summary = (
            df[type_col]
            .fillna("Unknown")
            .value_counts()
            .rename_axis("facility_type")
            .reset_index(name="facility_count")
        )
        summary.to_csv(OUTPUT_DIR / "facility_type_summary.csv", index=False)

    if province_col:
        df.groupby(province_col, dropna=False).size().reset_index(
            name="facility_count"
        ).to_csv(OUTPUT_DIR / "province_summary.csv", index=False)

    if municipality_col:
        group_columns = [c for c in [province_col, municipality_col] if c]
        df.groupby(group_columns, dropna=False).size().reset_index(
            name="facility_count"
        ).to_csv(OUTPUT_DIR / "municipality_summary.csv", index=False)

    # Density here means facility count by administrative area,
    # not population-adjusted healthcare access.
    print("Facility and regional summaries saved.")


# --------------------------------------------------
# 5. Prepare map-ready facility records
# --------------------------------------------------


def prepare_map_data(df, columns):
    lat_col = columns["latitude"]
    lon_col = columns["longitude"]

    if not lat_col or not lon_col:
        raise ValueError("Latitude and longitude columns are required for map export.")

    geo = df.copy()
    geo[lat_col] = pd.to_numeric(geo[lat_col], errors="coerce")
    geo[lon_col] = pd.to_numeric(geo[lon_col], errors="coerce")

    valid = geo[lat_col].between(41, 84) & geo[lon_col].between(-142, -50)
    geo = geo.loc[valid].copy()
    if geo.empty:
        raise ValueError("No facilities have valid Canadian coordinates.")

    geo.to_csv(OUTPUT_DIR / "facility_map.csv", index=False)

    print(f"Map-ready facilities: {len(geo)}")
    print(f"Rows without valid Canadian coordinates: {len(df) - len(geo)}")

    return geo


def export_geojson(geo, columns):
    features = []
    for _, row in geo.iterrows():
        properties = {}
        for field in ("name", "type", "province", "municipality", "address"):
            column = columns[field]
            value = row[column] if column else None
            properties[field] = None if pd.isna(value) else str(value)
        exposed = row.get("inside_hazard_zone")
        distance = row.get("distance_to_hazard_km")
        properties["inside_hazard_zone"] = None if pd.isna(exposed) else bool(exposed)
        properties["distance_to_hazard_km"] = (
            None if pd.isna(distance) else float(distance)
        )
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [
                        float(row[columns["longitude"]]),
                        float(row[columns["latitude"]]),
                    ],
                },
                "properties": properties,
            }
        )
    document = {
        "type": "FeatureCollection",
        "metadata": {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "source": geo.attrs.get("source"),
            "hazardAnalysisPerformed": "inside_hazard_zone" in geo.columns,
            "disclaimer": (
                "Prototype facility inventory, not verified operational capacity. "
                "Null exposure means not assessed. Outside supplied hazard polygons "
                "does not establish safety, road access, or available capacity."
            ),
        },
        "features": features,
    }
    path = OUTPUT_DIR / "facilities.geojson"
    path.write_text(json.dumps(document, allow_nan=False), encoding="utf-8")
    print("GeoJSON saved:", path)


# --------------------------------------------------
# 6. Hazard exposure and nearby alternatives
# --------------------------------------------------


def analyze_hazard_exposure(geo, columns):
    if not HAZARD_FILE.exists():
        for filename in ("hazard_exposure.csv", "alternative_facilities.csv"):
            (OUTPUT_DIR / filename).unlink(missing_ok=True)
        print(
            f"No hazard file found at {HAZARD_FILE}. "
            "Skipping hazard exposure and alternative analysis."
        )
        return geo

    import geopandas as gpd
    from shapely.geometry import Point

    lat_col = columns["latitude"]
    lon_col = columns["longitude"]
    name_col = columns["name"]

    facilities = gpd.GeoDataFrame(
        geo.copy(),
        geometry=[Point(lon, lat) for lon, lat in zip(geo[lon_col], geo[lat_col])],
        crs="EPSG:4326",
    )

    hazards = gpd.read_file(HAZARD_FILE)

    if hazards.empty:
        raise ValueError("Hazard file is empty.")

    if hazards.crs is None:
        raise ValueError("Hazard file has no CRS. Set its real CRS before analysis.")

    if (
        hazards.geometry.isna().any()
        or hazards.geometry.is_empty.any()
        or not hazards.geometry.is_valid.all()
        or not hazards.geometry.geom_type.isin(["Polygon", "MultiPolygon"]).all()
    ):
        raise ValueError("Hazard file must contain valid, non-empty polygons.")

    hazards = hazards.to_crs("EPSG:4326")

    # Keep a stable hazard identifier and combine overlapping polygons
    # into a union to avoid duplicating facilities.
    hazard_union = hazards.geometry.union_all()

    exposed_mask = facilities.geometry.intersects(hazard_union)
    facilities["inside_hazard_zone"] = exposed_mask

    # Compute distance in a projected CRS suitable for Canada.
    facilities_m = facilities.to_crs("EPSG:3347")
    hazard_m = (
        gpd.GeoSeries([hazard_union], crs="EPSG:4326").to_crs("EPSG:3347").iloc[0]
    )

    facilities["distance_to_hazard_km"] = (
        facilities_m.geometry.distance(hazard_m) / 1000
    )

    facilities["needs_review"] = facilities["inside_hazard_zone"] | (
        facilities["distance_to_hazard_km"] <= ALTERNATIVE_RADIUS_KM
    )

    facilities.drop(columns="geometry").to_csv(
        OUTPUT_DIR / "hazard_exposure.csv", index=False
    )

    print("Facilities intersecting hazard zones:", int(exposed_mask.sum()))

    # Candidate alternatives: facilities outside hazard zones.
    # Proximity is geographic, not proof of spare capacity or accessibility.
    safe_candidates = facilities.loc[~exposed_mask].copy()

    rows = []
    if exposed_mask.any() and not safe_candidates.empty:
        exposed = facilities.loc[exposed_mask]

        for idx, source in exposed.iterrows():
            distances = haversine_km(
                source[lat_col],
                source[lon_col],
                safe_candidates[lat_col],
                safe_candidates[lon_col],
            )

            nearest = (
                distances[distances <= ALTERNATIVE_RADIUS_KM].sort_values().head(3)
            )

            for candidate_idx, distance in nearest.items():
                candidate = safe_candidates.loc[candidate_idx]
                rows.append(
                    {
                        "affected_facility": (
                            source[name_col] if name_col else str(idx)
                        ),
                        "candidate_alternative": (
                            candidate[name_col] if name_col else str(candidate_idx)
                        ),
                        "distance_km": round(float(distance), 2),
                        "capacity_verified": False,
                    }
                )

    pd.DataFrame(
        rows,
        columns=[
            "affected_facility",
            "candidate_alternative",
            "distance_km",
            "capacity_verified",
        ],
    ).to_csv(OUTPUT_DIR / "alternative_facilities.csv", index=False)

    print("Candidate alternatives saved for review:", len(rows))
    result = facilities.drop(columns="geometry")
    result.attrs = geo.attrs.copy()
    return result


# --------------------------------------------------
# 7. Run pipeline
# --------------------------------------------------


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df = ingest_data()
    columns = detect_columns(df)

    analyze_quality(df)
    analyze_facilities(df, columns)

    geo = prepare_map_data(df, columns)
    geo = analyze_hazard_exposure(geo, columns)
    export_geojson(geo, columns)

    print("\nPipeline complete. Output directory:", OUTPUT_DIR.resolve())


if __name__ == "__main__":
    main()
