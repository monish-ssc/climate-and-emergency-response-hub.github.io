"""River gauge status: level datums, one-reading spikes, flat placeholder values, short records."""

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
    "discharge": {"month_means": list(range(1, 13)), "median_annual_peak": 20.0},
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
        c = flood.assess_station(readings("DISCHARGE", [25.0 + 0.1 * (i % 3) for i in range(24)]), BASE)
        self.assertEqual(c["status"], "flood")
        self.assertEqual(c["percentile_for_month"], 100)

    def test_one_spike_is_not_a_flood(self):
        # 05CE002 Kneehills Creek: last reading 31.9 m3/s, recent mean about 0.75
        values = [0.7 + 0.01 * (i % 4) for i in range(23)] + [31.9]
        c = flood.assess_station(readings("DISCHARGE", values), BASE)
        self.assertNotEqual(c["status"], "flood")

    def test_flat_flow_readings_are_not_measurements(self):
        # 05CE011 Renwick Creek: exactly 2.05 m3/s for hours while WSC showed 0.001
        c = flood.assess_station(readings("DISCHARGE", [2.05] * 24), {**BASE, "discharge": {
            "month_means": list(range(12)), "median_annual_peak": 2.01}})
        self.assertTrue(c["flat_readings"])
        self.assertEqual(c["status"], "unknown")

    def test_short_record_cannot_reach_flood(self):
        # 05OE014 Pansy Drain: 6 years of history
        short = {**BASE, "discharge": {"month_means": [1, 2, 3, 4, 5, 6], "median_annual_peak": 1.74}}
        c = flood.assess_station(readings("DISCHARGE", [7.7 + 0.05 * (i % 3) for i in range(24)]), short)
        self.assertEqual(c["status"], "above_normal")
        self.assertIsNone(c["percentile_for_month"])  # not used by the flood score
        self.assertIn("short record", c["note"])

    def test_trickle_ranked_high_for_the_season_is_not_flood_relevant(self):
        # Ridge Creek, October: 0.053 m3/s, 98th percentile, typical yearly peak 4.69 m3/s
        low = {**BASE, "discharge": {"month_means": [0.004 * i for i in range(1, 13)], "median_annual_peak": 4.69}}
        c = flood.assess_station(readings("DISCHARGE", [0.053 + 0.001 * (i % 3) for i in range(24)]), low)
        self.assertEqual(c["status"], "above_normal")
        self.assertIsNone(c["percentile_for_month"])
        self.assertEqual(c["percentile_raw"], 100)

    def test_lake_outlet_near_its_yearly_peak_still_counts(self):
        # Mossy River below Dauphin Lake: 43 m3/s against a typical yearly peak of 36
        lake = {**BASE, "discharge": {"month_means": list(range(5, 41, 3)), "median_annual_peak": 36.0}}
        c = flood.assess_station(readings("DISCHARGE", [43.3 + 0.1 * (i % 3) for i in range(24)]), lake)
        self.assertEqual(c["status"], "flood")


if __name__ == "__main__":
    unittest.main()
