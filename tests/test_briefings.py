"""build_briefings.py: grounding checks and the AI path, with the model call simulated (no network)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data"))
import build_briefings as bb  # noqa: E402

bb.PAUSE = 0  # no pause between simulated requests


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

    def test_local_model_is_called_without_a_key(self):
        sent = []

        class Reply:
            status, headers = 200, {"Content-Type": "application/json"}

            def read(self):
                return json.dumps({"choices": [{"message": {"content": json.dumps(answer())}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        saved = bb.urllib.request.urlopen
        bb.urllib.request.urlopen = lambda req, **k: sent.append(req) or Reply()
        try:
            self.assertEqual(bb.call_model("", "qwen2.5:7b-8k", "[F1] fact")["title"], answer()["title"])
        finally:
            bb.urllib.request.urlopen = saved
        self.assertIsNone(sent[0].get_header("Authorization"))
        self.assertTrue(sent[0].full_url.startswith(bb.ENDPOINT))


class RateLimitTest(unittest.TestCase):
    def test_rate_limited_model_is_retried_after_waiting(self):
        import io
        tries, waits = [], []

        def busy_once(*a, **k):
            tries.append(a[1])
            if len(tries) == 1:
                raise bb.urllib.error.HTTPError(bb.ENDPOINT, 429, "Too Many Requests", {"Retry-After": "7"},
                                                io.BytesIO(b'{"error":"rate-limited upstream"}'))
            return answer()

        saved = (bb.call_model, bb.time.sleep)
        bb.call_model, bb.time.sleep = busy_once, waits.append
        try:
            self.assertEqual(bb.ask("key", "google/gemma-4-31b-it:free", "[F1] fact")["title"], answer()["title"])
        finally:
            bb.call_model, bb.time.sleep = saved
        self.assertEqual(tries, ["google/gemma-4-31b-it:free"] * 2)  # same model, second try
        self.assertEqual(waits, [7.0])  # waited as long as the service asked


class LocalModelBuildTest(unittest.TestCase):
    """The model runs on the workflow's runner: no token, a time budget, and replay-only runs."""

    def run_build(self, fake, **kwargs):
        saved = (bb.call_model, bb.OUT, bb.load_pinned)
        bb.call_model = fake
        bb.OUT = Path(tempfile.mkdtemp()) / "briefings.json"
        bb.load_pinned = lambda: {}
        try:
            return bb.build(**kwargs)
        finally:
            bb.call_model, bb.OUT, bb.load_pinned = saved

    def test_no_model_server_means_templates_after_one_try(self):
        calls = []

        def down(*a, **k):
            calls.append(a)
            raise bb.urllib.error.URLError("connection refused")

        doc = self.run_build(down, max_calls=10)
        self.assertEqual(len(calls), 1)  # stops at the first refused connection
        self.assertEqual(doc["counts"]["ai"], 0)
        self.assertTrue(any(b.get("aiRejected") == ["model server not reachable"] for b in doc["briefings"]))

    def test_only_replay_asks_about_replay_moments_only(self):
        asked = []
        doc = self.run_build(lambda t, m, facts_text, feedback=None: asked.append(facts_text) or answer(
            situation="The replayed score shows the risk level for this place [F3].",
            action="Review the local emergency plan and contact lists."), max_calls=3, only=["replay"])
        self.assertEqual(len(asked), 3)
        self.assertTrue(all("Historical replay" in f for f in asked))
        ai = [b for b in doc["briefings"] if b["model"] != "template"]
        self.assertTrue(ai and all(b["kind"] == "replay" for b in ai))
        self.assertEqual(ai[0]["modelLabel"], bb.model_label(bb.MODELS[0]))
        self.assertEqual(ai[0]["provider"], bb.PROVIDER)

    def test_time_budget_stops_new_calls(self):
        clock = iter(range(0, 10000, 100))  # every reading of the clock is 100 s later
        saved = bb.time.monotonic
        bb.time.monotonic = lambda: next(clock)
        try:
            asked = []
            self.run_build(lambda *a, **k: asked.append(a) or answer(), max_calls=50, time_budget=250)
        finally:
            bb.time.monotonic = saved
        self.assertLess(len(asked), 5)


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
        follow = answer(action="Monitor the status of the evacuation order for nearby properties.")
        self.assertEqual(bb.check(follow, facts(), "moderate"), [])
        order = answer(action="Issue an evacuation order for nearby properties.")
        self.assertTrue(any("risk level is moderate" in p for p in bb.check(order, facts(), "moderate")))

    def test_french_number_format_and_clock_times_are_accepted(self):
        f = facts()
        f.add("The fire is about 1,684 ha and 17.3 km away.")
        a = answer(situation="At 1:00 p.m. the fire was 17.3 km away [F4].")
        a["publicAlert"]["fr"] = {"headline": "Feu de forêt", "body": "Le feu est à 17,3 km et couvre environ 1 684 ha (13 h 00).",
                                  "whatToDo": ["Soyez prêts."]}
        self.assertEqual(bb.check(a, f), [])
        a["publicAlert"]["fr"]["body"] = "Environ 2 500 personnes."
        self.assertTrue(any("2500" in p for p in bb.check(a, f)))

    def test_small_model_citation_styles_are_normalized(self):
        a = bb.normalize_citations(answer(situation="Risk score is 51 out of 100 [2]. About 707 people live there (F3)."))
        self.assertEqual(a["situation"], "Risk score is 51 out of 100 [F2]. About 707 people live there [F3].")
        self.assertEqual(bb.check(a, facts()), [])
        kept = bb.normalize_citations(answer(situation="Fires (10) burned in [2023] [F2]."))
        self.assertEqual(kept["situation"], "Fires (10) burned in [2023] [F2].")

    def test_citing_a_fact_that_does_not_exist_is_caught(self):
        bad = answer(situation="Clinton's wildfire risk score is 51 out of 100 [F9].")
        self.assertTrue(any("do not exist: F9" in p for p in bb.check(bad, facts())))

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
