import copy
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from data import hurricane_pipeline as hurricane


NOW = datetime(2026, 10, 28, 20, 0, tzinfo=timezone.utc)
REGION = {"id": "halifax", "label": "Halifax, NS"}
CSV = (
    "SID,SEASON,NAME,ISO_TIME,LAT,LON,TRACK_TYPE\n"
    ",Year,,,degrees_north,degrees_east,\n"
    "one,2024,ALPHA,2024-10-01 00:00:00,44.6,-63.5,main\n"
    "one,2024,ALPHA,2024-10-01 06:00:00,44.7,-63.6,main\n"
    "spur,2024,ALPHA,2024-10-01 06:00:00,44.7,-63.6,spur\n"
    "far,2025,BETA,2025-10-01 00:00:00,10,-40,main\n"
    "current,2026,GAMMA,2026-10-01 00:00:00,44.6,-63.5,main\n"
)


def track(**properties):
    return {
        "type": "Feature", "id": "track_ALPHA",
        "geometry": {
            "type": "LineString", "coordinates": [[-65, 44.6488], [-62, 44.6488]],
        },
        "properties": {
            "active": True, "latest_publication": True, "type": "track",
            "publication_datetime": hurricane.flood.iso(NOW - timedelta(hours=1)),
            "storm_origin_year": 2026, "basin": "AL", "storm_number": 1,
            "storm_name": "ALPHA", **properties,
        },
    }


def alert(**properties):
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [
            [[-64, 44], [-63, 44], [-63, 45], [-64, 44]],
        ]},
        "properties": {
            "alert_name_en": "hurricane warning", "alert_type": "warning",
            "publication_datetime": hurricane.flood.iso(NOW - timedelta(hours=1)),
            "expiration_datetime": hurricane.flood.iso(NOW + timedelta(hours=1)),
            "status_en": "continued", **properties,
        },
    }


class HurricanePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "history.csv"
        self.path.write_text(CSV, encoding="utf-8")
        self.history, self.observed = hurricane.read_history(self.path, NOW)

    def test_pagination_follows_relative_next_link_even_for_short_pages(self):
        docs = [
            {"type": "FeatureCollection", "features": [track()], "numberMatched": 2,
             "links": [{"rel": "next", "href": "?offset=1&f=json"}]},
            {"type": "FeatureCollection", "features": [track()], "numberMatched": 2},
        ]
        with patch.object(hurricane.flood, "get_json", side_effect=docs) as get:
            result = hurricane.fetch(
                "hurricanes-track-realtime", active="true", latest_publication="true"
            )
        self.assertEqual(len(result), 2)
        self.assertIn("active=true", get.call_args_list[0].args[0])
        self.assertTrue(get.call_args_list[1].args[0].endswith("?offset=1&f=json"))

    def test_malformed_incomplete_and_looping_responses_fail(self):
        for doc, error in [
            ({}, ValueError),
            ({"type": "FeatureCollection", "features": [], "numberMatched": 10},
             RuntimeError),
            ({"type": "FeatureCollection", "features": [],
              "links": [{"rel": "next", "href": "?f=json&limit=1000"}]}, RuntimeError),
        ]:
            with self.subTest(doc=doc):
                with patch.object(hurricane.flood, "get_json", return_value=doc):
                    with self.assertRaises(error):
                        hurricane.fetch("hurricanes-track-realtime")

    def test_filters_inactive_and_superseded_tracks(self):
        valid = track()
        result = hurricane.current_tracks([
            valid, track(active=False), track(latest_publication=False),
            track(type="response_zone"),
        ], NOW)
        self.assertEqual(result, [valid])

    def test_stale_future_or_missing_timestamp_fails_not_empty_success(self):
        for stamp in [
            hurricane.flood.iso(NOW - timedelta(hours=25)),
            hurricane.flood.iso(NOW + timedelta(hours=1)),
            "2026-10-28T19:00:00",
        ]:
            with self.subTest(stamp=stamp):
                with self.assertRaises((ValueError, RuntimeError)):
                    hurricane.current_tracks([track(publication_datetime=stamp)], NOW)
        feature = track()
        del feature["properties"]["publication_datetime"]
        with self.assertRaises(KeyError):
            hurricane.current_tracks([feature], NOW)

    def test_distance_checks_segments_not_only_vertices(self):
        geometry = {"type": "LineString", "coordinates": [[-5, 0], [5, 0]]}
        self.assertAlmostEqual(hurricane.track_distance_km(geometry, 0, 0), 0)
        self.assertAlmostEqual(
            hurricane.track_distance_km(geometry, 1, 0), 111.195, places=2
        )
        self.assertAlmostEqual(
            hurricane.track_distance_km(geometry, 0, 10), 555.975, places=2
        )

    def test_multiline_and_dateline_distance(self):
        geometry = {"type": "MultiLineString", "coordinates": [
            [[170, 0], [-170, 0]], [[0, 40], [1, 40]],
        ]}
        self.assertAlmostEqual(hurricane.track_distance_km(geometry, 0, 180), 0)

    def test_invalid_geometry_fails(self):
        for geometry in [
            {"type": "Point", "coordinates": [0, 0]},
            {"type": "LineString", "coordinates": []},
            {"type": "LineString", "coordinates": [[0, 0], [200, 0]]},
            {"type": "LineString", "coordinates": [[0, 0], [float("nan"), 0]]},
        ]:
            with self.subTest(geometry=geometry):
                with self.assertRaises(ValueError):
                    hurricane.track_distance_km(geometry, 0, 0)

    def test_history_skips_units_current_season_and_spurs_deduplicates_sid(self):
        self.assertEqual(len(self.history), 3)
        rows = hurricane.historical_storms(self.history, REGION)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["storm_id"], "one")
        self.assertEqual(rows[0]["season"], 2024)
        self.assertTrue(rows[0]["observed_at"].endswith("Z"))

    def test_invalid_history_fails(self):
        for contents in [
            CSV.replace("44.6,-63.5,main", "999,-63.5,main"),
            CSV.replace("2026-10-01", "2027-10-01"),
            "SID,SEASON,NAME,ISO_TIME,LAT,LON,TRACK_TYPE\n,Year,,,,,\n",
        ]:
            self.path.write_text(contents, encoding="utf-8")
            with self.assertRaises(ValueError):
                hurricane.read_history(self.path, NOW)

    def test_history_normalizes_unwrapped_dateline_longitudes(self):
        self.path.write_text(CSV.replace("10,-40,main", "10,181,main"), encoding="utf-8")
        frame, _ = hurricane.read_history(self.path, NOW)
        self.assertEqual(frame.loc[frame["SID"] == "far", "LON"].iloc[0], -179)

    def test_fresh_history_cache_does_not_download(self):
        with (
            patch.object(hurricane, "HISTORY_CACHE", self.path),
            patch.object(hurricane.urllib.request, "urlopen") as download,
        ):
            frame, observed = hurricane.load_history(NOW, self.path)
        download.assert_not_called()
        self.assertEqual(len(frame), len(self.history))
        self.assertEqual(observed, self.observed)

    def test_refresh_validates_download_before_replacing_cache(self):
        with (
            patch.object(hurricane, "HISTORY_CACHE", self.path),
            patch.object(
                hurricane.urllib.request, "urlopen",
                return_value=io.BytesIO(CSV.encode("utf-8")),
            ) as download,
        ):
            frame, _ = hurricane.load_history(NOW, self.path, refresh=True)
        download.assert_called_once()
        self.assertEqual(len(frame), len(self.history))
        self.assertEqual(self.path.read_text(), CSV)
        self.assertFalse(self.path.with_suffix(".csv.part").exists())

    def test_bad_refresh_preserves_cache_and_removes_partial_download(self):
        with (
            patch.object(hurricane, "HISTORY_CACHE", self.path),
            patch.object(
                hurricane.urllib.request, "urlopen",
                return_value=io.BytesIO(b"invalid csv"),
            ),
        ):
            with self.assertRaises(ValueError):
                hurricane.load_history(NOW, self.path, refresh=True)
        self.assertEqual(self.path.read_text(), CSV)
        self.assertFalse(self.path.with_suffix(".csv.part").exists())

    def test_alerts_exclude_ended_expired_and_unrelated(self):
        valid = alert()
        with patch.object(hurricane, "fetch", return_value=[
            valid, alert(status_en="ended"),
            alert(expiration_datetime=hurricane.flood.iso(NOW)),
            alert(alert_name_en="snowfall warning"),
        ]):
            self.assertEqual(hurricane.regional_alerts(REGION, NOW), [valid])

    def test_score_bounds_thresholds_and_no_duplicate_alert_contribution(self):
        warning = alert()["properties"]
        for distance, alerts, expected, level in [
            (300, [], 0, "low"), (200, [], 23, "low"),
            (150, [], 35, "moderate"), (50, [], 58, "high"),
            (300 * (1 - 25 / 70), [], 25, "moderate"),
            (300 * (1 - 50 / 70), [], 50, "high"),
            (300 * (1 - 45 / 70), [warning], 75, "critical"),
            (0, [warning, warning], 100, "critical"),
        ]:
            with self.subTest(distance=distance):
                score, actual_level, factors = hurricane.score_region(
                    [{"distance_km": distance}], alerts, 100, (2023, 2025)
                )
                self.assertEqual((score, actual_level), (expected, level))
                self.assertEqual(factors[-1]["contribution"], 0)
                self.assertEqual(factors[1]["value"], 1 if alerts else 0)
                self.assertEqual(score, round(sum(f["contribution"] for f in factors)))

    def test_empty_live_response_is_explicit_and_contract_valid(self):
        with patch.object(hurricane, "fetch", return_value=[]):
            doc, tracks, past, alerts = hurricane.run_region(
                REGION, NOW, [], self.history, self.observed, hurricane.flood.iso(NOW)
            )
        self.assertEqual(hurricane.flood.validate(doc), "valid")
        self.assertEqual(doc["hazard"], "severe_storm")
        self.assertIn("does not establish no risk", doc["assessment"]["summary"])
        self.assertEqual(doc["assessment"]["score"], 0)
        self.assertEqual((tracks, alerts), ([], []))
        self.assertEqual(len(past), 1)

    def test_storm_count_deduplicates_multiple_forecast_products(self):
        features = [track(), copy.deepcopy(track())]
        with patch.object(hurricane, "fetch", return_value=[alert()]):
            doc, rows, _, alerts = hurricane.run_region(
                REGION, NOW, features, self.history, self.observed,
                hurricane.flood.iso(NOW),
            )
        self.assertTrue(doc["assessment"]["summary"].startswith("1 active storm(s)"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(alerts[0]["_geometry"]["type"], "Polygon")
        self.assertEqual(hurricane.flood.validate(doc), "valid")

    def test_main_writes_contracts_and_geojson_without_touching_flood_outputs(self):
        out = Path(self.temp.name) / "pipeline"
        samples = Path(self.temp.name) / "samples"
        out.mkdir()
        flood_output = out / "stations.geojson"
        flood_output.write_text("unchanged", encoding="utf-8")
        with (
            patch.object(hurricane, "HISTORY_CACHE", self.path),
            patch.object(hurricane, "OUT_DIR", out),
            patch.object(hurricane, "SAMPLES_DIR", samples),
            patch.object(hurricane, "load_history", return_value=(
                self.history, self.observed,
            )),
            patch.object(hurricane, "fetch", return_value=[]),
            patch("sys.argv", ["hurricane_pipeline.py"]),
        ):
            hurricane.main()
        self.assertEqual(flood_output.read_text(), "unchanged")
        responses = list(samples.glob("*_severe_storm.json"))
        self.assertEqual(len(responses), 3)
        self.assertEqual(
            {path.name for path in responses},
            {"vancouver_severe_storm.json", "calgary_severe_storm.json",
             "halifax_severe_storm.json"},
        )
        self.assertFalse((out / "risk").exists())
        for path in responses:
            self.assertEqual(hurricane.flood.validate(json.loads(path.read_text())), "valid")
        for name in [
            "hurricane_tracks_latest.geojson", "hurricane_historical_storms.geojson",
            "hurricane_active_alerts.geojson",
        ]:
            self.assertEqual(json.loads((out / name).read_text())["type"], "FeatureCollection")

    def test_upstream_error_does_not_overwrite_existing_outputs(self):
        out = Path(self.temp.name) / "pipeline"
        samples = Path(self.temp.name) / "samples"
        with (
            patch.object(hurricane, "HISTORY_CACHE", self.path),
            patch.object(hurricane, "OUT_DIR", out),
            patch.object(hurricane, "SAMPLES_DIR", samples),
            patch.object(hurricane, "load_history", return_value=(
                self.history, self.observed,
            )),
            patch.object(hurricane, "fetch", side_effect=RuntimeError("upstream down")),
            patch("sys.argv", ["hurricane_pipeline.py"]),
        ):
            with self.assertRaisesRegex(RuntimeError, "upstream down"):
                hurricane.main()
        self.assertFalse(out.exists())
        self.assertFalse(samples.exists())


if __name__ == "__main__":
    unittest.main()
