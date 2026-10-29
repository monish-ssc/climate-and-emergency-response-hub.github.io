"""build_briefings.py: grounding checks and the AI path, with the model call simulated (no network)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data"))
import build_briefings as bb  # noqa: E402


def facts():
    f = bb.Facts()
    f.add("Place: Clinton, BC. Hazard: wildfire.")
    f.add("Automated wildfire risk score: 51 out of 100, level high (screening score, not an official warning).")
    f.add("Clinton has about 707 residents (Statistics Canada 2025 estimate).", "Statistics Canada", "https://www.statcan.gc.ca")
    return f


def answer(situation="Clinton's wildfire risk score is 51 out of 100, which is high [F2]. About 707 people live there [F3].",
           action="Prepare a reception centre plan for 707 residents."):
    alert = {"headline": "Wildfire risk is high", "body": "Be ready to leave if officials ask.", "whatToDo": ["Pack a kit."]}
    return {"title": "High wildfire risk - Clinton, BC", "situation": situation,
            "actions": [{"priority": 1, "action": action, "rationale": "The score is high [F2]."}],
            "confidence": "medium", "publicAlert": {"en": alert, "fr": alert}}


class ModelReplyTest(unittest.TestCase):
    def test_reply_that_is_not_json_names_the_status(self):
        class Reply:
            status, headers = 200, {"Content-Type": "text/html"}

            def read(self):
                return b"<html>sign in</html>"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        saved = bb.urllib.request.urlopen
        bb.urllib.request.urlopen = lambda *a, **k: Reply()
        try:
            with self.assertRaisesRegex(ValueError, r"not JSON \(HTTP 200"):
                bb.call_model("token", "model", "[F1] fact")
        finally:
            bb.urllib.request.urlopen = saved


class CheckTest(unittest.TestCase):
    def test_grounded_answer_passes(self):
        self.assertEqual(bb.check(answer(), facts()), [])

    def test_invented_number_is_caught(self):
        bad = answer(situation="About 3,000 people may need to leave [F3].")
        self.assertTrue(any("3000" in p for p in bb.check(bad, facts())))

    def test_actions_too_strong_for_a_low_risk_are_caught(self):
        low = answer(action="Prepare reception centres and transport so they can open quickly.")
        self.assertTrue(any("risk level is low" in p for p in bb.check(low, facts(), "low")))
        self.assertEqual(bb.check(low, facts(), "high"), [])
        ok = answer(action="Review the local evacuation plan and contact lists.")
        self.assertEqual(bb.check(ok, facts(), "low"), [])

    def test_french_number_format_and_clock_times_are_accepted(self):
        f = facts()
        f.add("The fire is about 1,684 ha and 17.3 km away.")
        a = answer(situation="At 1:00 p.m. the fire was 17.3 km away [F4].")
        a["publicAlert"]["fr"] = {"headline": "Feu de forêt", "body": "Le feu est à 17,3 km et couvre environ 1 684 ha (13 h 00).",
                                  "whatToDo": ["Soyez prêts."]}
        self.assertEqual(bb.check(a, f), [])
        a["publicAlert"]["fr"]["body"] = "Environ 2 500 personnes."
        self.assertTrue(any("2500" in p for p in bb.check(a, f)))

    def test_missing_citation_is_caught(self):
        self.assertTrue(any("cite" in p for p in bb.check(answer(situation="Risk is high."), facts())))


class BuildTest(unittest.TestCase):
    def test_retry_then_accept_and_template_fallback(self):
        calls = []

        def fake(token, model, facts_text, feedback=None):
            calls.append(feedback)
            # first answer invents a number, the retry is grounded
            if feedback is None:
                return answer(situation="About 9,999 people are at risk [F3].")
            return answer(situation="The replayed score shows the risk level for this place [F3].",
                          action="Review the local emergency plan and contact lists.")

        saved = (bb.call_model, bb.OUT)
        bb.call_model = fake
        bb.OUT = Path(tempfile.mkdtemp()) / "briefings.json"
        try:
            import os
            os.environ["GITHUB_TOKEN"] = "test"
            doc = bb.build(max_calls=2)
        finally:
            bb.call_model, bb.OUT = saved
        ai = [b for b in doc["briefings"] if b["model"] != "template" and not b.get("pinned")]
        self.assertEqual(len(ai), 1)  # budget of 2 calls = one subject (retry used the second)
        self.assertIn("numbers not in the facts", calls[1])
        b = ai[0]["briefing"]
        self.assertTrue(b["requiresHumanReview"])
        self.assertEqual(b["contractVersion"], "1.0")
        # every other subject still has a template briefing
        self.assertEqual(doc["counts"]["template"], doc["counts"]["subjects"] - 1 - doc["counts"]["pinned"])

    def test_saved_replay_briefings_are_used_as_they_are(self):
        subject = bb.replay_subjects(bb.Reference())[0]
        saved_entry = {"key": subject["key"], "model": "openai/gpt-4.1-mini", "generatedAt": "2026-10-29T22:00:00Z",
                       "briefing": {"title": "Saved draft", "situation": "x [F2]", "actions": []},
                       "publicAlert": {"en": {}, "fr": {}}, "facts": ["fact one"], "reviewed": True}
        calls = []
        saved = (bb.call_model, bb.OUT, bb.load_pinned)
        bb.call_model = lambda *a, **k: calls.append(a) or answer()
        bb.OUT = Path(tempfile.mkdtemp()) / "briefings.json"
        bb.load_pinned = lambda: {subject["key"]: saved_entry}
        try:
            import os
            os.environ["GITHUB_TOKEN"] = "test"
            doc = bb.build(max_calls=1)
        finally:
            bb.call_model, bb.OUT, bb.load_pinned = saved
        b = next(x for x in doc["briefings"] if x["key"] == subject["key"])
        self.assertEqual(b["briefing"]["title"], "Saved draft")
        self.assertTrue(b["pinned"] and b["reviewed"])
        self.assertEqual(b["facts"], ["fact one"])  # the facts its citations point to
        self.assertEqual(doc["counts"]["pinned"], 1)
        self.assertEqual(len(calls), 1)  # the one call went to another subject, not the saved one

    def test_template_advisory_names_the_open_reception_centre(self):
        subject = next(s for s in bb.replay_subjects(bb.Reference())
                       if s["key"] == "replay/2026_BC_2026-C40983/28")
        alert = bb.template_alert(subject)
        self.assertIn("evacuation was reported on Jul 24", alert["en"]["body"])
        self.assertIn("McArthur Island", alert["en"]["body"])
        self.assertIn("24 juillet", alert["fr"]["body"])


if __name__ == "__main__":
    unittest.main()
