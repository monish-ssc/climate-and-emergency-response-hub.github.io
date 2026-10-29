import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from data import build_live_layers as live
from data import restore_published_snapshot as restore

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)


def alert(name, zone, published="2026-10-29T14:00:00Z", status="issued",
          expires="2026-10-29T20:00:00Z", code="WDW"):
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[-100.123456, 50.123456], [-100, 51], [-99, 50], [-100.123456, 50.123456]]]},
        "properties": {
            "alert_code": code, "alert_name_en": name, "alert_type": "warning",
            "risk_colour_en": "yellow", "status_en": status, "feature_id": zone,
            "feature_name_en": f"Zone {zone}", "province": "ab",
            "publication_datetime": published, "expiration_datetime": expires,
            "alert_text_en": "long text",
        },
    }


class AlertLayerTests(unittest.TestCase):
    def test_categories_put_each_alert_in_one_control(self):
        cases = {
            "tornado warning": "tornado",
            "severe thunderstorm watch": "thunderstorm",
            "storm surge warning": "storm_surge",
            "hurricane watch": "tropical",
            "special air quality statement": "fire_weather",
            "heat warning": "fire_weather",
            "snow squall warning": "snowstorm",
            "freezing rain warning": "snowstorm",
            "wind warning": "wind",
            "rainfall warning": "rain",
            "fog advisory": "other",
            None: "other",
        }
        for name, category in cases.items():
            with self.subTest(name=name):
                self.assertEqual(live.alert_category(name), category)

    def test_alerts_are_filtered_deduplicated_and_trimmed(self):
        features = [
            alert("wind warning", "z1", published="2026-10-29T10:00:00Z"),
            alert("wind warning", "z1", published="2026-10-29T14:00:00Z"),
            alert("wind warning", "z2", status="ended"),
            alert("wind warning", "z3", expires="2026-10-29T14:59:00Z"),
            alert("rainfall warning", "z4", code="RFW"),
            alert("fog advisory", "z5", code="FGA"),
        ]
        with patch.object(live.flood, "fetch", return_value=features):
            layers = live.alert_layers(NOW)

        self.assertEqual(set(layers), set(live.ALERT_LAYERS))
        wind, observed = layers["wind_alerts"]
        self.assertEqual(len(wind), 1)
        self.assertEqual(wind[0]["properties"]["publication_datetime"], "2026-10-29T14:00:00Z")
        self.assertEqual(observed, "2026-10-29T14:00:00Z")
        self.assertNotIn("alert_text_en", wind[0]["properties"])
        self.assertEqual(wind[0]["geometry"]["coordinates"][0][0], [-100.1235, 50.1235])
        rain, _ = layers["rain_alerts"]
        self.assertEqual(sorted(f["properties"]["category"] for f in rain), ["other", "rain"])
        empty, observed = layers["tornado_alerts"]
        self.assertEqual((empty, observed), ([], "2026-10-29T15:00:00Z"))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data_dir = Path(temp.name)
        self.live_dir = self.data_dir / "pipeline" / "live"
        for name, value in (
            ("DATA_DIR", self.data_dir),
            ("LIVE_DIR", self.live_dir),
            ("MANIFEST", self.live_dir / "manifest.json"),
        ):
            patcher = patch.object(live, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, fires=None):
        def fail(*_):
            raise RuntimeError("source down")

        with (
            patch.object(live, "fire_layer", fires or fail),
            patch.object(live, "alert_layers", lambda now: {i: ([], live.flood.iso(now)) for i in live.ALERT_LAYERS}),
            patch.object(live, "river_layer", fail),
            patch.object(live, "hurricane_layer", fail),
        ):
            return {e["id"]: e for e in live.build(NOW)["layers"]}

    def test_failed_source_without_previous_data_is_an_empty_error_layer(self):
        layers = self.build()

        self.assertEqual([e["id"] for e in layers.values()], [i for i, _, _ in live.LAYERS])
        self.assertEqual(layers["river_conditions"]["status"], "error")
        self.assertEqual(layers["river_conditions"]["error"], "source down")
        self.assertEqual(layers["river_conditions"]["featureCount"], 0)
        self.assertEqual(layers["wind_alerts"]["status"], "ok")
        self.assertEqual(layers["wind_alerts"]["expiresAt"], "2026-10-29T16:30:00Z")
        doc = json.loads((self.live_dir / "river_conditions.geojson").read_text())
        self.assertEqual(doc, {"type": "FeatureCollection", "features": []})

    def test_failed_source_keeps_the_previous_layer_as_stale(self):
        point = live.feature({"type": "Point", "coordinates": [-120, 50]}, {"stage": "OC"})
        first = self.build(fires=lambda now: {"active_fires": ([point], "2026-10-29T14:30:00Z")})
        self.assertEqual(first["active_fires"]["status"], "ok")

        second = self.build()

        fires = second["active_fires"]
        self.assertEqual(fires["status"], "stale")
        self.assertEqual(fires["featureCount"], 1)
        self.assertEqual(fires["observedAt"], "2026-10-29T14:30:00Z")
        self.assertEqual(fires["generatedAt"], first["active_fires"]["generatedAt"])
        doc = json.loads((self.live_dir / "active_fires.geojson").read_text())
        self.assertEqual(len(doc["features"]), 1)


class RiverLayerTests(unittest.TestCase):
    def test_only_budgeted_station_histories_are_fetched(self):
        stations = [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-100 - i, 50]},
             "properties": {"STATION_NUMBER": f"S{i}", "STATION_NAME": f"River {i}",
                            "PROV_TERR_STATE_LOC": "AB", "DRAINAGE_AREA_GROSS": None}}
            for i in range(3)
        ]
        readings = [
            {"type": "Feature", "geometry": None,
             "properties": {"STATION_NUMBER": f"S{i}", "DATETIME": f"2026-10-29T{h:02d}:00:00Z",
                            "LEVEL": None, "DISCHARGE": 10.0 + h}}
            for i in range(3) for h in range(9, 15)
        ]
        history = {"month": 10, "discharge": {"month_means": [1.0] * 10, "median_annual_peak": 100.0},
                   "level": {"month_means": [], "median_annual_peak": None}}

        def fetch(collection, **_):
            return stations if collection == "hydrometric-stations" else readings + readings[:1]

        with tempfile.TemporaryDirectory() as temp, \
                patch.object(live.flood, "BASELINE_CACHE", Path(temp) / "baselines.json"), \
                patch.object(live.flood, "fetch", side_effect=fetch), \
                patch.object(live.flood, "fetch_baseline", return_value=history) as fetch_baseline:
            layer = live.river_layer(NOW, budget=2)["river_conditions"]
            features, observed = layer
            cached = json.loads((Path(temp) / "baselines.json").read_text())

        self.assertEqual(fetch_baseline.call_count, 2)
        self.assertEqual(sorted(cached), ["S0", "S1"])
        by_station = {f["properties"]["station_number"]: f["properties"] for f in features}
        self.assertEqual(by_station["S0"]["baseline"], "ready")
        self.assertEqual(by_station["S0"]["status"], "very_high")
        self.assertEqual(by_station["S0"]["readings"], 6)
        self.assertEqual(by_station["S0"]["trend"], "rising")
        self.assertEqual(by_station["S2"]["baseline"], "pending")
        self.assertEqual(by_station["S2"]["status"], "unknown")
        self.assertEqual(observed, "2026-10-29T14:00:00Z")


class RestoreTests(unittest.TestCase):
    def test_restore_refuses_paths_outside_data(self):
        with self.assertRaisesRegex(ValueError, "outside data"):
            restore.target("../index.html")
        self.assertEqual(restore.target("pipeline/live/a.geojson"),
                         restore.DATA_DIR / "pipeline" / "live" / "a.geojson")


if __name__ == "__main__":
    unittest.main()
