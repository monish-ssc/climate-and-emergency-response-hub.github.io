"""Level-only river gauges are not compared with history (live and historical datums can differ)."""

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data"))
import flood_pipeline as flood  # noqa: E402


def readings(column, values):
    times = pd.date_range("2026-10-28T14:00Z", periods=len(values), freq="h")
    other = "LEVEL" if column == "DISCHARGE" else "DISCHARGE"
    return pd.DataFrame({"DATETIME": times.strftime("%Y-%m-%dT%H:%M:%SZ"), column: values, other: [None] * len(values)})


BASE = {
    "level": {"month_means": [0.5, 0.6, 0.7, 0.8, 0.9, 1.0], "median_annual_peak": 1.797},
    "discharge": {"month_means": [1, 2, 3, 4, 5, 6], "median_annual_peak": 20.0},
}


class LevelGaugeTest(unittest.TestCase):
    def test_level_above_historical_peak_is_not_a_flood(self):
        # 08LF027 Deadman River, Oct 29 2026: 4.27 m live, steady; yearly maxima 1.2-2.5 m
        c = flood.assess_station(readings("LEVEL", [4.265] * 24), BASE)
        self.assertEqual(c["metric"], "level")
        self.assertEqual(c["status"], "unknown")
        self.assertIsNone(c["percentile_for_month"])
        self.assertIsNone(c["median_annual_peak"])
        self.assertEqual(c["trend"], "steady")

    def test_level_trend_is_still_reported(self):
        c = flood.assess_station(readings("LEVEL", [1.0] * 12 + [1.3] * 12), BASE)
        self.assertEqual(c["trend"], "rising")

    def test_discharge_is_still_compared_with_history(self):
        c = flood.assess_station(readings("DISCHARGE", [25.0] * 24), BASE)
        self.assertEqual(c["status"], "flood")
        self.assertEqual(c["percentile_for_month"], 100)


if __name__ == "__main__":
    unittest.main()
