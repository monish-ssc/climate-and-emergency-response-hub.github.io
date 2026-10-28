"""Explore annual population estimates on 2021 census subdivision boundaries."""

import argparse
import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests

# local boundary file is required for mapping CSD boundaries

URL = "https://www150.statcan.gc.ca/n1/tbl/csv/17100155-eng.zip"
SOURCE_PAGE = "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=1710015501"
DATA_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = DATA_DIR / "population"
EXPECTED_FILE = "17100155.csv"
REQUIRED_COLUMNS = {
    "ref_date",
    "geo",
    "dguid",
    "uom",
    "scalar_factor",
    "value",
    "status",
}
TABLE_COLUMNS = REQUIRED_COLUMNS | {
    "uom_id",
    "scalar_id",
    "vector",
    "coordinate",
    "symbol",
    "terminated",
    "decimals",
}
MISSING_VALUES = {"", ".", "..", "...", "x", "F", "..s"}
CSD_PATTERN = r"2021A0005(\d{7})"
AREA_CRS = "EPSG:3347"
LOGGER = logging.getLogger(__name__)


def read_population_zip(content: bytes) -> pd.DataFrame:
    """Require the documented table, not the first CSV in the archive."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if archive.namelist().count(EXPECTED_FILE) != 1:
            raise ValueError(f"ZIP must contain exactly one {EXPECTED_FILE}.")
        with archive.open(EXPECTED_FILE) as file:
            frame = pd.read_csv(
                file,
                dtype=str,
                keep_default_na=False,
                encoding="utf-8-sig",
                low_memory=False,
            )
    frame.columns = [
        re.sub(r"[^a-z0-9]+", "_", str(col).strip().lower()).strip("_")
        for col in frame.columns
    ]
    if frame.columns.duplicated().any():
        raise ValueError("Duplicate normalized population columns.")
    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"Missing population columns: {sorted(missing)}")
    # This table has no age/sex/statistic dimensions: each CSD row is a total.
    # Reject schema drift rather than accidentally summing another series.
    unexpected = set(frame.columns) - TABLE_COLUMNS
    if unexpected:
        raise ValueError(f"Unexpected table dimensions/columns: {sorted(unexpected)}")
    return frame.loc[~frame.eq("").all(axis=1)].reset_index(drop=True)


def select_population(frame: pd.DataFrame) -> pd.DataFrame:
    """Select only 2021 CSD total estimates and preserve unavailable values."""
    frame = frame.copy()
    for column in frame.columns:
        frame[column] = frame[column].astype("string").str.strip()
    csduid = frame["dguid"].str.extract(f"^{CSD_PATTERN}$", expand=False)
    frame["csduid"] = csduid
    frame = frame.loc[csduid.notna()].copy()
    if frame.empty:
        raise ValueError("No 2021 census subdivision estimates found.")
    if not frame["uom"].eq("Persons").all():
        raise ValueError("Population estimates must have UOM Persons.")
    if not frame["scalar_factor"].eq("units").all():
        raise ValueError("Population estimates must use scalar factor units.")
    if not frame["ref_date"].str.fullmatch(r"\d{4}").all():
        raise ValueError("Population reference dates must be annual YYYY values.")

    text = frame["value"]
    unavailable = text.isin(MISSING_VALUES) | frame["status"].isin(
        MISSING_VALUES - {""}
    )
    # E (use with caution), p and r are quality/revision flags, not missing data.
    numbers = pd.to_numeric(text.mask(unavailable), errors="coerce")
    invalid = ~unavailable & (
        numbers.isna() | ~np.isfinite(numbers) | numbers.lt(0) | numbers.mod(1).ne(0)
    )
    if invalid.any():
        raise ValueError(
            f"Invalid population values: {text.loc[invalid].head().tolist()}"
        )
    frame["population"] = numbers.astype("Float64")
    frame["year"] = frame["ref_date"].astype(int)
    if frame.duplicated(["csduid", "year"]).any():
        raise ValueError("Duplicate CSD/year totals; verify source dimensions.")
    return (
        frame[["csduid", "dguid", "geo", "year", "population", "status"]]
        .sort_values(["csduid", "year"])
        .reset_index(drop=True)
    )


def compare_years(
    population: pd.DataFrame,
    year: int,
    baseline_year: int,
) -> pd.DataFrame:
    if baseline_year >= year:
        raise ValueError("Baseline year must precede the analysis year.")
    available = set(population["year"])
    if year not in available or baseline_year not in available:
        raise ValueError(
            f"Requested years not available; choose from {sorted(available)}"
        )
    latest = population.loc[population["year"].eq(year)].copy()
    baseline = population.loc[
        population["year"].eq(baseline_year),
        [
            "csduid",
            "population",
            "status",
        ],
    ].rename(columns={"population": "baseline_population", "status": "baseline_status"})
    result = latest.merge(baseline, on="csduid", how="outer", validate="one_to_one")
    # A missing row is not a zero-population community.
    labels = population.drop_duplicates("csduid", keep="last").set_index("csduid")
    for column in ("dguid", "geo"):
        result[column] = result[column].fillna(result["csduid"].map(labels[column]))
    result["year"] = year
    result["baseline_year"] = baseline_year
    result["population_change"] = result["population"] - result["baseline_population"]
    result["population_change_percent"] = (
        result["population_change"]
        / result["baseline_population"].where(result["baseline_population"].gt(0))
        * 100
    )
    return result


def load_polygons(path: Path) -> gpd.GeoDataFrame:
    layer = gpd.read_file(path)
    if layer.crs is None:
        raise ValueError(f"{path}: geographic input requires a CRS.")
    if layer.empty or layer.geometry.isna().any() or layer.geometry.is_empty.any():
        raise ValueError(f"{path}: expected non-empty polygon geometries.")
    if not layer.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        raise ValueError(f"{path}: only Polygon/MultiPolygon geometries supported.")
    if not layer.is_valid.all():
        raise ValueError(f"{path}: invalid geometries; repair the input explicitly.")
    return layer.to_crs(AREA_CRS)


def join_boundaries(
    summary: pd.DataFrame,
    boundaries: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    boundaries = boundaries.copy()
    boundaries.columns = [str(column).lower() for column in boundaries.columns]
    if boundaries.columns.duplicated().any():
        raise ValueError("Duplicate boundary columns after normalization.")
    if "csduid" not in boundaries or "dguid" not in boundaries:
        raise ValueError("2021 boundaries require CSDUID and DGUID columns.")
    boundaries["csduid"] = boundaries["csduid"].astype("string").str.strip()
    expected = "2021A0005" + boundaries["csduid"]
    if (
        not boundaries["csduid"].str.fullmatch(r"\d{7}").fillna(False).all()
        or not boundaries["dguid"].astype("string").eq(expected).fillna(False).all()
    ):
        raise ValueError("Boundary CSDUID/DGUID must identify matching 2021 CSDs.")
    if boundaries["csduid"].duplicated().any():
        raise ValueError("Duplicate CSDUID boundaries; provide one feature per CSD.")
    boundaries = boundaries.to_crs(AREA_CRS)
    boundaries = boundaries[["csduid", "geometry"]]
    boundaries["area_km2"] = boundaries.area / 1_000_000
    if not (np.isfinite(boundaries["area_km2"]) & boundaries["area_km2"].gt(0)).all():
        raise ValueError("Boundaries must have finite, positive areas.")
    joined = boundaries.merge(summary, on="csduid", how="left", validate="one_to_one")
    joined["population_per_km2"] = joined["population"] / joined["area_km2"]
    unmatched = summary.loc[~summary["csduid"].isin(boundaries["csduid"])].copy()
    if len(unmatched):
        LOGGER.warning("%s population CSDs lack a supplied boundary.", len(unmatched))
    missing = joined["population"].isna().sum()
    if missing:
        LOGGER.warning("%s boundary CSDs have no available population.", missing)
    if not joined["csduid"].isin(summary["csduid"]).any():
        raise ValueError("No population CSD identifiers match the supplied boundaries.")
    return joined, unmatched


def estimate_exposure(
    areas: gpd.GeoDataFrame,
    hazards: gpd.GeoDataFrame | None,
) -> gpd.GeoDataFrame:
    result = areas.copy()
    for hazard in ("flood", "wildfire"):
        result[f"{hazard}_area_fraction"] = pd.Series(
            pd.NA,
            index=result.index,
            dtype="Float64",
        )
        result[f"{hazard}_exposed_population_estimate"] = pd.Series(
            pd.NA,
            index=result.index,
            dtype="Float64",
        )
    if hazards is None:
        LOGGER.warning("No hazard layer supplied; population exposure is unknown.")
        return result
    if "hazard" not in hazards:
        raise ValueError("Hazard polygons require a hazard field: flood or wildfire.")
    if (
        hazards["hazard"].isna().any()
        or not hazards["hazard"].isin(["flood", "wildfire"]).all()
    ):
        raise ValueError("Hazard labels must be flood or wildfire.")
    hazards = hazards.to_crs(areas.crs)
    for hazard in ("flood", "wildfire"):
        polygons = hazards.loc[hazards["hazard"].eq(hazard)]
        if polygons.empty:
            LOGGER.warning("No %s layer supplied; its exposure is unknown.", hazard)
            continue
        # Dissolve overlapping zones so a person/area is not counted twice.
        footprint = polygons.geometry.union_all()
        fraction = (areas.geometry.intersection(footprint).area / areas.area).clip(0, 1)
        result[f"{hazard}_area_fraction"] = fraction
        result[f"{hazard}_exposed_population_estimate"] = areas["population"] * fraction
    return result


def file_source(path: Path) -> dict:
    return {
        "file": str(path.resolve()),
        "modifiedAt": datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc
        ).isoformat(),
    }


def run_pipeline(
    boundaries_path: Path,
    output_dir: Path = OUTPUT_DIR,
    hazards_path: Path | None = None,
    year: int | None = None,
    baseline_year: int | None = None,
) -> dict:
    boundaries = load_polygons(boundaries_path)
    hazards = load_polygons(hazards_path) if hazards_path is not None else None
    response = requests.get(URL, timeout=120)
    response.raise_for_status()
    retrieved_at = datetime.now(timezone.utc).isoformat()
    raw = read_population_zip(response.content)
    population = select_population(raw)
    years = sorted(population["year"].unique())
    year = years[-1] if year is None else year
    previous_years = [int(value) for value in years if value < year]
    if baseline_year is None:
        if not previous_years:
            raise ValueError("No earlier year available for population change.")
        baseline_year = previous_years[-1]
    summary = compare_years(population, year, baseline_year)
    areas, unmatched = join_boundaries(summary, boundaries)
    areas = estimate_exposure(areas, hazards)
    metadata = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dataMode": "live",
        "source": {
            "name": "Statistics Canada table 17-10-0155-01",
            "url": SOURCE_PAGE,
            "downloadUrl": URL,
            "retrievedAt": retrieved_at,
            "observedAt": f"{year}-07-01T00:00:00Z",
        },
        "boundaries": file_source(boundaries_path),
        "hazards": file_source(hazards_path) if hazards_path is not None else None,
        "year": int(year),
        "baselineYear": int(baseline_year),
        "boundaryVintage": 2021,
        "unmatchedPopulationCSDs": len(unmatched),
        "boundaryCSDsWithoutPopulation": int(areas["population"].isna().sum()),
        "units": {
            "population": "persons",
            "area_km2": "km2",
            "population_per_km2": "persons/km2",
            "population_change": "persons",
            "population_change_percent": "percent",
            "hazard_area_fraction": "fraction",
            "exposed_population_estimate": "persons",
        },
        "disclaimer": (
            "Exploratory estimates, not an operational risk assessment. "
            "Exposure assumes uniform population within each whole CSD polygon, "
            "including water. Hazard inputs must cover the supplied study area; "
            "no intersection means zero only for a supplied hazard type. "
            "Missing hazard types and unavailable populations remain unknown. "
            "Flood and wildfire estimates overlap and must not be added."
        ),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "population_source.zip").write_bytes(response.content)
    raw.to_csv(output_dir / "population_raw.csv", index=False)
    pd.DataFrame(
        {
            "missing_count": population.isna().sum(),
            "missing_percent": population.isna().mean() * 100,
            "unique_values": population.nunique(),
        }
    ).to_csv(output_dir / "population_quality_report.csv", index_label="column")
    population.to_csv(output_dir / "population_clean.csv", index=False)
    # Retain populations without boundaries in tabular rankings and change output.
    summary.sort_values("population", ascending=False, na_position="last").to_csv(
        output_dir / "population_by_csd.csv",
        index=False,
    )
    unmatched.to_csv(output_dir / "population_unmatched.csv", index=False)
    areas.drop(columns="geometry").to_csv(
        output_dir / "population_analysis.csv", index=False
    )
    document = json.loads(areas.to_crs("EPSG:4326").to_json(na="null", drop_id=True))
    document["metadata"] = metadata
    (output_dir / "population.geojson").write_text(
        json.dumps(document, allow_nan=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "population_metadata.json").write_text(
        json.dumps(metadata, allow_nan=False, indent=2),
        encoding="utf-8",
    )
    LOGGER.info("Population pipeline complete: %s", output_dir.resolve())
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--boundaries", type=Path, required=True, help="Local 2021 CSD layer"
    )
    parser.add_argument(
        "--hazards", type=Path, help="Optional flood/wildfire polygon layer"
    )
    parser.add_argument("--year", type=int, help="Analysis year (default: latest)")
    parser.add_argument(
        "--baseline-year", type=int, help="Default: previous available year"
    )
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    run_pipeline(
        args.boundaries,
        args.output_dir,
        args.hazards,
        args.year,
        args.baseline_year,
    )


if __name__ == "__main__":
    main()
