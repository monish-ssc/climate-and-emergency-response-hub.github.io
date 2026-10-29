import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from data import healthcare_data_pipeline as pipeline


class HealthcarePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.output_patch = patch.object(pipeline, "OUTPUT_DIR", self.output)
        self.output_patch.start()
        self.addCleanup(self.output_patch.stop)
        self.hazard_patch = patch.object(
            pipeline, "HAZARD_FILE", self.output / "hazards.geojson"
        )
        self.hazard_patch.start()
        self.addCleanup(self.hazard_patch.stop)

    def frame(self):
        return pd.DataFrame(
            {
                "facility_name": ["Affected", "Nearby", "Far away"],
                "latitude": [49.0, 49.1, 51.0],
                "longitude": [-123.0, -123.0, -123.0],
                "city": ["Example", "Example", "Other"],
            }
        )

    def test_paths_are_script_relative(self):
        self.assertEqual(pipeline.DATA_DIR, Path(pipeline.__file__).resolve().parent)

    def test_csv_encodings(self):
        for encoding in ("utf-8-sig", "cp1252"):
            content = "facility_name\nCentre \u2014 sant\u00e9\n".encode(encoding)
            frame = pipeline.read_csv(content)
            self.assertEqual(frame["facility_name"].iloc[0], "Centre \u2014 sant\u00e9")

    def test_inventory_columns_prefer_standard_type_and_full_address(self):
        frame = self.frame().assign(
            source_facility_type="active acute hospital",
            odhf_facility_type="Hospitals",
            street_no="123",
            source_format_str_address="123 Example Street",
        )
        columns = pipeline.detect_columns(frame)
        self.assertEqual(columns["type"], "odhf_facility_type")
        self.assertEqual(columns["address"], "source_format_str_address")

    def test_municipality_summary_without_province(self):
        frame = self.frame()
        pipeline.analyze_facilities(frame, pipeline.detect_columns(frame))
        summary = pd.read_csv(self.output / "municipality_summary.csv")
        self.assertEqual(dict(zip(summary["city"], summary["facility_count"])),
                         {"Example": 2, "Other": 1})

    def test_geojson_without_hazards_has_unknown_exposure(self):
        frame = self.frame()
        frame.attrs["source"] = {"datasetId": pipeline.DATASET_ID}
        columns = pipeline.detect_columns(frame)
        geo = pipeline.prepare_map_data(frame, columns)
        for filename in ("hazard_exposure.csv", "alternative_facilities.csv"):
            (self.output / filename).write_text("stale", encoding="utf-8")
        result = pipeline.analyze_hazard_exposure(geo, columns)
        pipeline.export_geojson(result, columns)
        document = json.loads(
            (self.output / "facilities.geojson").read_text(encoding="utf-8")
        )
        self.assertEqual(document["type"], "FeatureCollection")
        self.assertEqual(len(document["features"]), 3)
        self.assertFalse(document["metadata"]["hazardAnalysisPerformed"])
        self.assertEqual(document["metadata"]["source"], frame.attrs["source"])
        first = document["features"][0]
        self.assertEqual(first["geometry"]["coordinates"], [-123.0, 49.0])
        self.assertEqual(first["properties"]["name"], "Affected")
        self.assertIsNone(first["properties"]["province"])
        self.assertIsNone(first["properties"]["inside_hazard_zone"])
        self.assertIsNone(first["properties"]["distance_to_hazard_km"])
        self.assertFalse((self.output / "hazard_exposure.csv").exists())
        self.assertFalse((self.output / "alternative_facilities.csv").exists())

    def test_invalid_coordinates_are_filtered(self):
        frame = self.frame()
        frame.loc[1, "latitude"] = 0
        frame.loc[2, "longitude"] = float("nan")
        result = pipeline.prepare_map_data(frame, pipeline.detect_columns(frame))
        self.assertEqual(len(result), 1)

    def test_missing_or_unusable_coordinates_raise(self):
        frame = self.frame().drop(columns="latitude")
        with self.assertRaisesRegex(ValueError, "required"):
            pipeline.prepare_map_data(frame, pipeline.detect_columns(frame))
        frame = self.frame()
        frame["latitude"] = 0
        with self.assertRaisesRegex(ValueError, "No facilities"):
            pipeline.prepare_map_data(frame, pipeline.detect_columns(frame))

    @unittest.skipUnless(importlib.util.find_spec("geopandas"), "Optional GeoPandas")
    def test_hazard_exposure_and_alternative_radius(self):
        import geopandas as gpd
        from shapely.geometry import box

        gpd.GeoDataFrame(
            geometry=[box(-123.01, 48.99, -122.99, 49.01)], crs="EPSG:4326"
        ).to_file(pipeline.HAZARD_FILE, driver="GeoJSON")
        frame = self.frame()
        columns = pipeline.detect_columns(frame)
        geo = pipeline.prepare_map_data(frame, columns)
        result = pipeline.analyze_hazard_exposure(geo, columns)
        self.assertEqual(result["inside_hazard_zone"].tolist(), [True, False, False])
        alternatives = pd.read_csv(self.output / "alternative_facilities.csv")
        self.assertEqual(alternatives["candidate_alternative"].tolist(), ["Nearby"])
        self.assertTrue((alternatives["distance_km"] <= 50).all())
        pipeline.export_geojson(result, columns)
        document = json.loads(
            (self.output / "facilities.geojson").read_text(encoding="utf-8")
        )
        self.assertTrue(document["metadata"]["hazardAnalysisPerformed"])
        self.assertTrue(document["features"][0]["properties"]["inside_hazard_zone"])
        self.assertEqual(
            document["features"][0]["properties"]["distance_to_hazard_km"], 0
        )

        geo = geo.iloc[[0, 2]]
        pipeline.analyze_hazard_exposure(geo, columns)
        alternatives = pd.read_csv(self.output / "alternative_facilities.csv")
        self.assertTrue(alternatives.empty)


if __name__ == "__main__":
    unittest.main()
