import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

from data import build_population_map as builder


class PopulationMapTests(unittest.TestCase):
    def test_land_area_density_true_zero_missing_values_and_metadata(self):
        boundaries = gpd.GeoDataFrame({
            "CSDUID": ["1001001", "1001002", "1001003"],
            "DGUID": [f"2021A0005100100{i}" for i in (1, 2, 3)],
            "CSDNAME": ["First", "Second", "Third"],
            "LANDAREA": [2.0, 4.0, 5.0], "PRUID": ["10"] * 3,
        }, geometry=[box(i * 1000, 0, (i + 1) * 1000, 1000) for i in range(3)],
            crs="EPSG:3347")
        records = pd.DataFrame({
            "REF_DATE": ["2024", "2024"], "GEO": ["First", "Second"],
            "DGUID": ["2021A00051001001", "2021A00051001002"],
            "UOM": ["Persons"] * 2, "SCALAR_FACTOR": ["units"] * 2,
            "VALUE": ["100", "0"], "STATUS": ["", ""],
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("17100155.csv", records.to_csv(index=False))
            with patch.object(builder, "complete_boundaries", return_value=root / "boundary.shp"), \
                 patch.object(builder.gpd, "read_file", return_value=boundaries):
                builder.build(root / "boundary.shp", root, archive)
            summary = json.loads((root / "population_summary.json").read_text())
            document = json.loads((root / "population_density.geojson").read_text())
        communities = {row["csduid"]: row for row in summary["communities"]}
        self.assertEqual(communities["1001001"]["density"], 50)
        self.assertEqual(communities["1001002"]["population"], 0)
        self.assertEqual(communities["1001002"]["density"], 0)
        self.assertIsNone(communities["1001003"]["population"])
        self.assertIsNone(communities["1001003"]["density"])
        self.assertEqual(summary["metadata"]["totalPopulation"], 100)
        self.assertEqual(summary["metadata"]["missingPopulationCount"], 1)
        self.assertEqual(summary["metadata"]["year"], 2024)
        self.assertEqual(document["metadata"], summary["metadata"])
        self.assertEqual(len(document["features"]), 3)
        self.assertEqual(summary["metadata"]["boundaries"]["file"], "boundary.shp")


if __name__ == "__main__":
    unittest.main()
