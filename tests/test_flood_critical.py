"""Flood score: "critical" needs a sign of escalation, not just a river at its typical yearly peak."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data"))
import flood_pipeline as flood  # noqa: E402


def station(pct=100, peak_pct=105, trend="steady", status="flood"):
    return {"percentile_for_month": pct, "pct_of_flood_peak": peak_pct, "trend": trend, "status": status}


class CriticalNeedsEscalationTest(unittest.TestCase):
    def test_river_at_its_typical_peak_stops_at_high(self):
        # like Mossy River below Dauphin Lake, Oct 29: top of its record for the month, at its typical yearly peak, steady, no alert
        score, level, factors = flood.score_region([station()], [])
        self.assertEqual((score, level), (flood.CAPPED_SCORE, "high"))
        cap = next(f for f in factors if f["key"] == "critical_needs_escalation")
        self.assertEqual(cap["contribution"], 0)  # the schema allows no negative points
        self.assertIn("Uncapped score", cap["explanation"])

    def test_an_official_alert_can_make_it_critical(self):
        alert = {"risk_colour_en": "orange", "alert_name_en": "Flood warning"}
        self.assertEqual(flood.score_region([station()], [alert])[1], "critical")

    def test_a_much_larger_flow_can_make_it_critical(self):
        self.assertEqual(flood.score_region([station(peak_pct=160)], [])[1], "critical")

    def test_a_river_still_rising_at_its_peak_can_make_it_critical(self):
        self.assertEqual(flood.score_region([station(trend="rising")], [])[1], "critical")

    def test_lower_scores_are_unchanged(self):
        score, level, factors = flood.score_region([station(pct=80, peak_pct=70, status="very_high")], [])
        self.assertLess(score, 75)
        self.assertFalse(any(f["key"] == "critical_needs_escalation" for f in factors))


if __name__ == "__main__":
    unittest.main()
