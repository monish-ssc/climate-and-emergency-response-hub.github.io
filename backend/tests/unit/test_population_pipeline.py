import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

from data import population_pipeline as pipeline


class PopulationPipelineTests(unittest.TestCase):
    def frame(self):
        return pd.DataFrame({
            "ref_date": ["2023", "2024", "2023", "2024"],
            "geo": ["First", "First", "Second", "Second"],
            "dguid": ["2021A00051001001"] * 2 + ["2021A00051001002"] * 2,
            "uom": ["Persons"] * 4,
            "scalar_factor": ["units"] * 4,
            "value": ["100", "120", "0", "50"],
            "status": ["", "r", "", "E"],
        })

    def archive(self, frame=None, filename=pipeline.EXPECTED_FILE):
        content = io.BytesIO()
        frame = self.frame() if frame is None else frame
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("17100155_MetaData.csv", "not the data")
            archive.writestr(filename, frame.to_csv(index=False))
        return content.getvalue()

    def boundaries(self):
        return gpd.GeoDataFrame({
            "CSDUID": ["1001001", "1001002"],
            "DGUID": ["2021A00051001001", "2021A00051001002"],
        }, geometry=[box(0, 0, 1000, 1000), box(1000, 0, 2000, 1000)],
            crs=pipeline.AREA_CRS)

    def areas(self):
        population = pipeline.select_population(self.frame())
        summary = pipeline.compare_years(population, 2024, 2023)
        areas, unmatched = pipeline.join_boundaries(summary, self.boundaries())
        self.assertTrue(unmatched.empty)
        return areas

    def test_ingestion_normalizes_headers_and_preserves_flags(self):
        frame = self.frame()
        frame.columns = [column.upper() for column in frame.columns]
        result = pipeline.read_population_zip(self.archive(frame))
        self.assertEqual(result["status"].tolist(), ["", "r", "", "E"])
        self.assertEqual(result["value"].tolist(), ["100", "120", "0", "50"])

    def test_ingestion_requires_expected_file_and_columns(self):
        with self.assertRaisesRegex(ValueError, "17100155.csv"):
            pipeline.read_population_zip(self.archive(filename="other.csv"))
        with self.assertRaisesRegex(ValueError, "Missing population columns"):
            pipeline.read_population_zip(self.archive(self.frame().drop(columns="value")))

    def test_rejects_unexpected_dimensions_and_duplicate_headers(self):
        for frame in (
            self.frame().assign(age="All ages"),
            self.frame().assign(VALUE="100"),
        ):
            with self.assertRaises(ValueError):
                pipeline.read_population_zip(self.archive(frame))

    def test_selection_excludes_aggregate_and_wrong_vintage_geographies(self):
        extra = self.frame().iloc[:1].copy()
        for dguid in ("2021A00001124", "2021A000210", "", "2016A00051001001"):
            extra["dguid"] = dguid
            result = pipeline.select_population(pd.concat([self.frame(), extra]))
            self.assertEqual(len(result), 4)

    def test_missing_symbols_do_not_become_zero(self):
        for symbol in pipeline.MISSING_VALUES:
            frame = self.frame()
            frame.loc[0, "value"] = symbol
            result = pipeline.select_population(frame)
            self.assertTrue(pd.isna(result.iloc[0]["population"]))
        for status in ("x", "F", ".."):
            frame = self.frame()
            frame.loc[0, "status"] = status
            result = pipeline.select_population(frame)
            self.assertTrue(pd.isna(result.iloc[0]["population"]))

    def test_invalid_population_is_an_explicit_error(self):
        for value in ("bad", "-1", "1.5", "inf", "NaN"):
            frame = self.frame()
            frame.loc[0, "value"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Invalid"):
                pipeline.select_population(frame)

    def test_units_scalars_dates_and_duplicate_totals_are_validated(self):
        for column, value in (
            ("uom", "Percent"), ("scalar_factor", "thousands"), ("ref_date", "2024-01"),
        ):
            frame = self.frame()
            frame.loc[0, column] = value
            with self.assertRaises(ValueError):
                pipeline.select_population(frame)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            pipeline.select_population(pd.concat([self.frame(), self.frame()]))

    def test_growth_zero_baseline_and_missing_rows(self):
        population = pipeline.select_population(self.frame())
        summary = pipeline.compare_years(population, 2024, 2023).set_index("csduid")
        self.assertEqual(summary.loc["1001001", "population_change"], 20)
        self.assertEqual(summary.loc["1001001", "population_change_percent"], 20)
        self.assertTrue(pd.isna(summary.loc["1001002", "population_change_percent"]))
        population = population.drop(index=1)
        summary = pipeline.compare_years(population, 2024, 2023).set_index("csduid")
        self.assertTrue(pd.isna(summary.loc["1001001", "population_change"]))
        self.assertEqual(summary.loc["1001001", "geo"], "First")
        for year, baseline in ((2023, 2024), (2024, 2000), (2000, 1999)):
            with self.assertRaises(ValueError):
                pipeline.compare_years(population, year, baseline)

    def test_area_density_and_id_join_not_name_join(self):
        areas = self.areas().set_index("csduid")
        self.assertEqual(areas.loc["1001001", "area_km2"], 1)
        self.assertEqual(areas.loc["1001001", "population_per_km2"], 120)

    def test_boundary_validation_and_unmatched_reporting(self):
        summary = pipeline.compare_years(pipeline.select_population(self.frame()), 2024, 2023)
        for boundaries in (
            self.boundaries().drop(columns="DGUID"),
            self.boundaries().assign(DGUID="2016A00051001001"),
            pd.concat([self.boundaries(), self.boundaries()]),
        ):
            with self.assertRaises(ValueError):
                pipeline.join_boundaries(summary, boundaries)
        with self.assertLogs(pipeline.LOGGER, level="WARNING"):
            areas, unmatched = pipeline.join_boundaries(summary, self.boundaries().iloc[:1])
        self.assertEqual(len(areas), 1)
        self.assertEqual(unmatched["csduid"].tolist(), ["1001002"])
        extra = self.boundaries().iloc[:1].assign(CSDUID="1001003", DGUID="2021A00051001003")
        boundaries = pd.concat([self.boundaries(), extra])
        with self.assertLogs(pipeline.LOGGER, level="WARNING"):
            areas, _ = pipeline.join_boundaries(summary, boundaries)
        self.assertTrue(pd.isna(areas.iloc[-1]["population"]))

    def test_exposure_union_fraction_no_overlap_and_unknown_type(self):
        hazards = gpd.GeoDataFrame(
            {"hazard": ["flood", "flood"]},
            geometry=[box(0, 0, 500, 1000), box(0, 0, 500, 1000)],
            crs=pipeline.AREA_CRS,
        )
        with self.assertLogs(pipeline.LOGGER, level="WARNING"):
            result = pipeline.estimate_exposure(self.areas(), hazards)
        self.assertEqual(result["flood_area_fraction"].tolist(), [0.5, 0])
        self.assertEqual(result["flood_exposed_population_estimate"].tolist(), [60, 0])
        self.assertTrue(result["wildfire_exposed_population_estimate"].isna().all())
        hazards["hazard"] = "wildfire"
        hazards.geometry = [box(-1, -1, 2001, 1001)] * 2
        with self.assertLogs(pipeline.LOGGER, level="WARNING"):
            result = pipeline.estimate_exposure(self.areas(), hazards)
        self.assertEqual(result["wildfire_area_fraction"].tolist(), [1, 1])

    def test_no_hazard_or_population_means_unknown_not_zero(self):
        with self.assertLogs(pipeline.LOGGER, level="WARNING"):
            result = pipeline.estimate_exposure(self.areas(), None)
        self.assertTrue(result["flood_area_fraction"].isna().all())
        areas = self.areas()
        areas.loc[0, "population"] = pd.NA
        hazards = gpd.GeoDataFrame(
            {"hazard": ["flood"]}, geometry=[box(0, 0, 500, 1000)],
            crs=pipeline.AREA_CRS,
        )
        with self.assertLogs(pipeline.LOGGER, level="WARNING"):
            result = pipeline.estimate_exposure(areas, hazards)
        self.assertTrue(pd.isna(result.iloc[0]["flood_exposed_population_estimate"]))
        with self.assertRaisesRegex(ValueError, "Hazard labels"):
            pipeline.estimate_exposure(areas, hazards.assign(hazard="storm"))

    def test_polygon_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boundaries.geojson"
            self.boundaries().to_file(path, driver="GeoJSON")
            self.assertEqual(len(pipeline.load_polygons(path)), 2)
        for layer in (
            self.boundaries().set_crs(None, allow_override=True),
            self.boundaries().iloc[:0],
            self.boundaries().set_geometry([box(0, 0, 0, 0)] * 2),
            self.boundaries().set_geometry([box(0, 0, 1, 1).centroid] * 2),
        ):
            with patch.object(pipeline.gpd, "read_file", return_value=layer):
                with self.assertRaises(ValueError):
                    pipeline.load_polygons(Path("invalid"))

    def test_end_to_end_exports_nulls_provenance_and_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boundary_path = root / "boundaries.geojson"
            self.boundaries().to_file(boundary_path, driver="GeoJSON")
            response = Mock(content=self.archive())
            output = root / "output"
            with patch.object(pipeline.requests, "get", return_value=response) as get:
                with self.assertLogs(pipeline.LOGGER, level="WARNING"):
                    metadata = pipeline.run_pipeline(boundary_path, output)
            get.assert_called_once_with(pipeline.URL, timeout=120)
            response.raise_for_status.assert_called_once()
            self.assertEqual(metadata["year"], 2024)
            self.assertEqual(metadata["baselineYear"], 2023)
            self.assertEqual(metadata["boundaryVintage"], 2021)
            self.assertEqual(metadata["source"]["observedAt"], "2024-07-01T00:00:00Z")
            document = json.loads((output / "population.geojson").read_text())
            self.assertEqual(len(document["features"]), 2)
            self.assertIsNone(
                document["features"][0]["properties"]["flood_exposed_population_estimate"]
            )
            self.assertEqual(document["metadata"], metadata)
            ranking = pd.read_csv(output / "population_by_csd.csv")
            self.assertEqual(ranking["population"].tolist(), [120, 50])
            self.assertTrue((output / "population_source.zip").exists())
            self.assertTrue((output / "population_quality_report.csv").exists())
            self.assertEqual(json.loads((output / "population_metadata.json").read_text()),
                             metadata)
            hazards_path = root / "hazards.geojson"
            gpd.GeoDataFrame(
                {"hazard": ["flood", "wildfire"]},
                geometry=[box(0, 0, 500, 1000), box(1000, 0, 2000, 1000)],
                crs=pipeline.AREA_CRS,
            ).to_file(hazards_path, driver="GeoJSON")
            with patch.object(pipeline.requests, "get", return_value=response):
                metadata = pipeline.run_pipeline(
                    boundary_path, output, hazards_path, year=2024, baseline_year=2023,
                )
            document = json.loads((output / "population.geojson").read_text())
            properties = document["features"][0]["properties"]
            self.assertAlmostEqual(properties["flood_exposed_population_estimate"], 60)
            self.assertAlmostEqual(properties["wildfire_exposed_population_estimate"], 0)
            self.assertEqual(metadata["hazards"]["file"], str(hazards_path.resolve()))
            coordinates = document["features"][0]["geometry"]["coordinates"][0][0]
            self.assertTrue(-180 <= coordinates[0] <= 180)
            self.assertTrue(-90 <= coordinates[1] <= 90)

    def test_provider_errors_propagate(self):
        response = Mock()
        response.raise_for_status.side_effect = pipeline.requests.HTTPError("upstream")
        with patch.object(pipeline, "load_polygons", return_value=self.boundaries()):
            with patch.object(pipeline.requests, "get", return_value=response):
                with self.assertRaises(pipeline.requests.HTTPError):
                    pipeline.run_pipeline(Path("boundaries"))


if __name__ == "__main__":
    unittest.main()
