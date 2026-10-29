"""Join official annual population estimates to local 2021 CSD boundaries.

Run: python data/build_population_map.py --boundaries data/raw/boundaries/lcsd000b21a_e.shp
The source ZIP stays in the ignored population directory. Compact, browser-ready
outputs go to data/pipeline and are included in the static site.
"""

import argparse
import io
import json
import struct
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

if __package__:
    from . import population_pipeline as population
else:
    import population_pipeline as population


PROVINCES = {
    "10": "Newfoundland and Labrador", "11": "Prince Edward Island",
    "12": "Nova Scotia", "13": "New Brunswick", "24": "Quebec",
    "35": "Ontario", "46": "Manitoba", "47": "Saskatchewan",
    "48": "Alberta", "59": "British Columbia", "60": "Yukon",
    "61": "Northwest Territories", "62": "Nunavut",
}
BOUNDARY_URL = "https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/boundary-limites/files-fichiers/lcsd000b21a_e.zip"
BOUNDARY_MIRROR = "https://data.source.coop/dataforcanada/d4c-datapkg-statistical/archive/statcan/boundaries/2021/lcsd000b21a_e.zip"


def complete_boundaries(path):
    with path.open("rb") as source:
        header = source.read(100)
    if len(header) == 100 and struct.unpack(">i", header[24:28])[0] * 2 <= path.stat().st_size:
        return path
    cache = population.OUTPUT_DIR / "boundaries"
    restored = cache / path.name
    if restored.exists():
        with restored.open("rb") as source:
            cached_header = source.read(100)
        if struct.unpack(">i", cached_header[24:28])[0] * 2 == restored.stat().st_size:
            return restored
    print("Local shapefile is truncated; downloading complete official boundaries", flush=True)
    response = requests.get(BOUNDARY_URL, timeout=240)
    if response.status_code == 403:
        print("Official archive returned 403; retrieving the same ZIP from Data for Canada", flush=True)
        response = requests.get(BOUNDARY_MIRROR, timeout=240)
    response.raise_for_status()
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    cache.mkdir(parents=True, exist_ok=True)
    for extension in ("shp", "shx", "dbf", "prj"):
        filename = path.with_suffix(f".{extension}").name
        member = next(name for name in archive.namelist() if Path(name).name == filename)
        (cache / filename).write_bytes(archive.read(member))
    return restored


def build(boundaries_path, output_dir, source_zip=None, tolerance=100):
    boundaries_path = complete_boundaries(boundaries_path)
    boundaries = gpd.read_file(boundaries_path)
    if boundaries.crs is None or boundaries.empty or boundaries.geometry.isna().any():
        raise ValueError("Boundaries need a CRS and non-empty polygon features")
    boundaries = boundaries.to_crs(population.AREA_CRS)
    invalid = ~boundaries.geometry.is_valid
    repaired = int(invalid.sum())
    print("Simplifying display boundaries before repairing source defects", flush=True)
    original = boundaries.geometry.copy()
    boundaries.geometry = original.simplify(tolerance, preserve_topology=False)
    collapsed = boundaries.geometry.is_empty
    if collapsed.any():
        boundaries.loc[collapsed, "geometry"] = original.loc[collapsed].simplify(
            tolerance, preserve_topology=True
        )
    invalid_display = ~boundaries.geometry.is_valid
    if repaired:
        print(f"Source had {repaired} invalid geometries; {int(invalid_display.sum())} need repair after simplification", flush=True)
    if invalid_display.any():
        boundaries.loc[invalid_display, "geometry"] = boundaries.loc[invalid_display, "geometry"].make_valid(
            method="structure", keep_collapsed=False
        )
    print("Boundary geometries ready; joining population estimates", flush=True)
    if boundaries.geometry.is_empty.any() or not boundaries.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        raise ValueError("Boundary repair must retain every polygon")
    required = {"CSDUID", "DGUID", "CSDNAME", "LANDAREA", "PRUID"}
    if not required.issubset(boundaries.columns):
        raise ValueError(f"Boundary file needs fields: {sorted(required)}")
    retrieved = datetime.now(timezone.utc).isoformat()
    if source_zip is None:
        source_zip = population.OUTPUT_DIR / "population_source.zip"
        response = requests.get(population.URL, timeout=120)
        response.raise_for_status()
        source_zip.parent.mkdir(parents=True, exist_ok=True)
        source_zip.write_bytes(response.content)
    else:
        retrieved = datetime.fromtimestamp(source_zip.stat().st_mtime, timezone.utc).isoformat()
    values = population.select_population(population.read_population_zip(source_zip.read_bytes()))
    years = sorted(values.year.unique())
    year = int(years[-1])
    totals = values.loc[values.year.eq(year), ["csduid", "population", "status"]]
    # Reuse the strict boundary identifier validation before selecting display fields.
    population.join_boundaries(totals, boundaries)
    areas = boundaries.rename(columns={
        "CSDUID": "csduid", "DGUID": "dguid", "CSDNAME": "name",
        "LANDAREA": "land_area_km2", "PRUID": "province_code",
    })[["csduid", "dguid", "name", "land_area_km2", "province_code", "geometry"]]
    areas = areas.merge(totals, on="csduid", how="left", validate="one_to_one")
    areas["land_area_km2"] = pd.to_numeric(areas.land_area_km2, errors="raise")
    if areas.land_area_km2.lt(0).any():
        raise ValueError("Negative official land area")
    areas["density"] = areas.population / areas.land_area_km2.where(areas.land_area_km2.gt(0))
    areas["province"] = areas.province_code.map(PROVINCES)
    areas["year"] = year
    print("Calculating map centres", flush=True)
    centres = areas.geometry.representative_point()
    centres = gpd.GeoSeries(centres, crs=areas.crs).to_crs("EPSG:4326")
    areas["centre_lat"] = centres.y.round(5)
    areas["centre_lon"] = centres.x.round(5)
    areas["density"] = areas.density.round(2)
    # Display geometry was simplified above. Values use official land area.
    if areas.geometry.is_empty.any() or not areas.geometry.is_valid.all():
        raise ValueError("Simplification produced invalid or empty boundaries")
    known = areas.population.notna()
    available_area = areas.loc[known, "land_area_km2"].sum()
    metadata = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dataMode": "observed", "year": year, "boundaryVintage": 2021,
        "source": {
            "name": "Statistics Canada annual population estimates, table 17-10-0155-01",
            "url": population.SOURCE_PAGE, "downloadUrl": population.URL,
            "observedAt": f"{year}-07-01", "retrievedAt": retrieved,
        },
        "boundaries": {"file": boundaries_path.name, "source": "Statistics Canada 2021 census subdivisions", "url": BOUNDARY_URL},
        "boundaryArchiveMirror": BOUNDARY_MIRROR,
        "densityMethod": "Annual population estimate divided by official LANDAREA, in km²",
        "geometrySimplificationMetres": tolerance,
        "sourceGeometriesRepaired": repaired,
        "totalPopulation": int(areas.population.sum()),
        "landAreaWithPopulationKm2": round(float(available_area), 4),
        "density": round(float(areas.population.sum() / available_area), 2),
        "boundaryCount": len(areas), "populatedBoundaryCount": int(known.sum()),
        "missingPopulationCount": int((~known).sum()),
        "missingDensityCount": int(areas.density.isna().sum()),
        "populationCSDsWithoutBoundary": int((~totals.csduid.isin(areas.csduid)).sum()),
    }
    document = json.loads(areas.to_crs("EPSG:4326").to_json(na="null", drop_id=True))
    # Five decimal degrees is enough for this simplified display layer.
    def round_coordinates(value):
        if isinstance(value, list):
            return [round_coordinates(item) for item in value]
        return round(value, 5) if isinstance(value, float) else value
    for feature in document["features"]:
        feature["geometry"]["coordinates"] = round_coordinates(feature["geometry"]["coordinates"])
    # Coordinate rounding can make tiny rings cross. Repair those display
    # geometries after rounding, then validate the exact published shapes.
    rounded = gpd.GeoDataFrame.from_features(document["features"], crs="EPSG:4326")
    invalid_rounded = ~rounded.geometry.is_valid
    metadata["roundedGeometriesRepaired"] = int(invalid_rounded.sum())
    if invalid_rounded.any():
        from shapely.geometry import mapping

        repaired_geometry = rounded.loc[invalid_rounded, "geometry"].make_valid(
            method="structure", keep_collapsed=False
        )
        if repaired_geometry.is_empty.any() or not repaired_geometry.geom_type.isin(["Polygon", "MultiPolygon"]).all():
            raise ValueError("Rounded boundary repair lost polygon geometry")
        for index, geometry in repaired_geometry.items():
            document["features"][index]["geometry"] = mapping(geometry)
        rounded.loc[invalid_rounded, "geometry"] = repaired_geometry
    if not rounded.geometry.is_valid.all():
        raise ValueError("Published geometry is invalid")
    document["metadata"] = metadata
    records = [{k: v for k, v in f["properties"].items()} for f in document["features"]]
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "population_density.geojson").write_text(
        json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":")), encoding="utf-8"
    )
    (output_dir / "population_summary.json").write_text(
        json.dumps({"metadata": metadata, "communities": records}, ensure_ascii=False,
                   allow_nan=False, separators=(",", ":")), encoding="utf-8"
    )
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boundaries", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=population.DATA_DIR / "pipeline")
    parser.add_argument("--source-zip", type=Path, help="Reuse a downloaded Statistics Canada source ZIP")
    parser.add_argument("--simplify-metres", type=float, default=100)
    args = parser.parse_args()
    if args.simplify_metres < 0:
        parser.error("--simplify-metres cannot be negative")
    build(args.boundaries, args.output_dir, args.source_zip, args.simplify_metres)
