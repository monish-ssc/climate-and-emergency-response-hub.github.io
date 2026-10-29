"""build_watchlist.py: screening communities from the live layers (no network)."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data"))
import build_watchlist as wl  # noqa: E402


def point(lon, lat, **props):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props}


class WatchlistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        live = self.tmp / "live"
        live.mkdir()
        fire = point(-121.6, 51.0, national_fire_id="F1", agency_fire_id="K1", stage="OC",
                     stage_label="out of control", size_ha=5000, radius_km=4.0, status_date="2026-07-23")
        (live / "active_fires.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [fire]}))
        river = point(-97.1, 49.9, station_name="RED RIVER", metric="discharge", status="very_high",
                      percentile_for_month=97, pct_of_flood_peak=85, trend="rising")
        (live / "river_conditions.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [river]}))
        pop = {"metadata": {"year": 2025}, "communities": [
            {"csduid": "1", "name": "Clinton", "province": "British Columbia", "population": 1200,
             "centre_lat": 51.09, "centre_lon": -121.59},
            {"csduid": "2", "name": "Winnipeg", "province": "Manitoba", "population": 850000,
             "centre_lat": 49.85, "centre_lon": -97.14},
            {"csduid": "3", "name": "Tiny", "province": "Manitoba", "population": 50,
             "centre_lat": 49.85, "centre_lon": -97.14},
        ]}
        (self.tmp / "pop.json").write_text(json.dumps(pop))
        self.saved = (wl.LIVE_DIR, wl.POPULATION, wl.OUT)
        wl.LIVE_DIR, wl.POPULATION, wl.OUT = live, self.tmp / "pop.json", live / "watchlist.json"

    def tearDown(self):
        wl.LIVE_DIR, wl.POPULATION, wl.OUT = self.saved

    def test_ranks_and_groups(self):
        doc = wl.build(now=datetime(2026, 7, 23, tzinfo=timezone.utc))
        self.assertEqual(doc["communitiesScreened"], 2)  # Tiny is under 1,000 people
        hazards = {g["hazard"]: g for g in doc["hotspots"]}
        self.assertEqual(hazards["wildfire"]["communities"][0]["name"], "Clinton")
        self.assertEqual(hazards["flood"]["communities"][0]["name"], "Winnipeg")
        self.assertGreater(hazards["wildfire"]["score"], 25)
        self.assertTrue(wl.OUT.exists())


if __name__ == "__main__":
    unittest.main()
