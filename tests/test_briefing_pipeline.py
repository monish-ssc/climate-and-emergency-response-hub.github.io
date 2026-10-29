import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from data import briefing_pipeline as bp

NOW = datetime(2026, 10, 29, 19, 0, tzinfo=timezone.utc)


def risk_doc(score=61, level="high", summary="2 fires within 150 km.", generated="2026-10-29T18:30:00Z"):
    return {
        "contractVersion": "1.0",
        "requestId": "r1",
        "region": {"id": "vancouver", "label": "Vancouver, BC"},
        "hazard": "wildfire",
        "assessment": {
            "score": score,
            "level": level,
            "summary": summary,
            "factors": [
                {"key": "fire_proximity", "label": "Closest fire", "value": 12.5, "unit": "km",
                 "contribution": 30, "explanation": "Fire X is 12.5 km away."},
            ],
        },
        "sources": [
            {"name": "CWFIS", "url": "https://cwfis.example/wfs", "observedAt": generated, "retrievedAt": generated},
            {"name": "CWFIS", "url": "https://cwfis.example/wfs", "observedAt": generated, "retrievedAt": generated},
            {"name": "No link", "observedAt": generated, "retrievedAt": generated},
        ],
        "dataMode": "live",
        "generatedAt": generated,
        "expiresAt": "2026-10-29T19:30:00Z",
    }


def gemini_reply(draft=None):
    draft = draft or {
        "title": "Vancouver wildfire briefing",
        "situation": "Risk is high at 61. Prototype screening, not an official warning.",
        "actions": [
            {"priority": 2, "action": "Review evacuation routes.", "rationale": "Fire 12.5 km away."},
            {"priority": 1, "action": "Check ECCC and BC alerts.", "rationale": "Screening is provisional."},
        ],
        "confidence": "high",
    }
    return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(draft)}]}}]}


class BriefingPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "pipeline" / "risk").mkdir(parents=True)
        self.risk_path = root / "pipeline" / "risk" / "vancouver_wildfire.json"
        self.write_risk(risk_doc())
        (root / "pipeline" / "risk" / "index.json").write_text(json.dumps({
            "builtAt": "2026-10-29T18:31:00Z",
            "entries": [{"regionId": "vancouver", "regionLabel": "Vancouver, BC", "hazard": "wildfire",
                         "score": 61, "level": "high", "path": "pipeline/risk/vancouver_wildfire.json",
                         "generatedAt": "2026-10-29T18:30:00Z"}],
        }))
        self.out_dir = root / "pipeline" / "briefing"
        patches = [
            patch.object(bp, "DATA_DIR", root),
            patch.object(bp, "RISK_INDEX", root / "pipeline" / "risk" / "index.json"),
            patch.object(bp, "OUT_DIR", self.out_dir),
            patch.object(bp, "OUT_INDEX", self.out_dir / "index.json"),
            patch.object(bp, "CALL_GAP_SECONDS", 0),
            patch.object(bp.time, "sleep", lambda *_: None),
            patch.dict(bp.os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def write_risk(self, doc):
        self.risk_path.write_text(json.dumps(doc))

    def run_main(self, *args, replies=None, now=NOW):
        calls = []

        def fake_post(url, body, headers, **kw):
            calls.append({"url": url, "body": body, "headers": headers})
            if replies is None:
                return gemini_reply()
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        with patch.object(bp, "post_json", side_effect=fake_post), patch.object(bp, "datetime", FixedDatetime):
            code = bp.main(list(args))
        return code, calls

    # ---- fingerprint / change detection ----

    def test_fingerprint_ignores_timestamps_but_tracks_scores(self):
        a = bp.fingerprint(risk_doc(generated="2026-10-29T10:00:00Z"))
        b = bp.fingerprint(risk_doc(generated="2026-10-29T11:00:00Z"))
        c = bp.fingerprint(risk_doc(score=62))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_first_run_generates_and_writes_valid_contract(self):
        code, calls = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["headers"]["x-goog-api-key"], "test-key")
        self.assertNotIn("test-key", calls[0]["url"])
        self.assertIn("gemini-3.8-flash", calls[0]["url"])
        doc = json.loads((self.out_dir / "vancouver_wildfire.json").read_text())
        bp.validate(doc)
        self.assertTrue(doc["requiresHumanReview"])
        self.assertEqual(doc["confidence"], "low", "'high' from the model must be capped")
        self.assertEqual([a["priority"] for a in doc["actions"]], [1, 2])
        self.assertEqual(doc["actions"][0]["action"], "Check ECCC and BC alerts.")
        self.assertEqual(doc["citations"], [{"title": "CWFIS", "url": "https://cwfis.example/wfs"}])
        self.assertEqual(doc["generatedAt"], "2026-10-29T19:00:00Z")
        index = json.loads((self.out_dir / "index.json").read_text())
        e = index["entries"][0]
        self.assertEqual((e["regionId"], e["hazard"], e["score"], e["level"]), ("vancouver", "wildfire", 61, "high"))
        self.assertEqual(e["path"], "pipeline/briefing/vancouver_wildfire.json")
        self.assertEqual(e["inputsFingerprint"], bp.fingerprint(risk_doc()))

    def test_unchanged_inputs_skip_the_model(self):
        self.run_main()
        code, calls = self.run_main(now=NOW + timedelta(hours=1))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        index = json.loads((self.out_dir / "index.json").read_text())
        self.assertEqual(index["entries"][0]["generatedAt"], "2026-10-29T19:00:00Z")

    def test_changed_score_regenerates(self):
        self.run_main()
        self.write_risk(risk_doc(score=80, level="critical"))
        code, calls = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn('"risk_score_0_to_100": 80', calls[0]["body"]["contents"][0]["parts"][0]["text"])

    def test_old_briefing_is_refreshed_after_a_day(self):
        self.run_main()
        _, calls = self.run_main(now=NOW + timedelta(hours=bp.MAX_AGE_HOURS + 1))
        self.assertEqual(len(calls), 1)

    def test_force_regenerates(self):
        self.run_main()
        _, calls = self.run_main("--force")
        self.assertEqual(len(calls), 1)

    def test_missing_output_file_regenerates_despite_index(self):
        self.run_main()
        (self.out_dir / "vancouver_wildfire.json").unlink()
        _, calls = self.run_main()
        self.assertEqual(len(calls), 1)

    # ---- no key / dry run ----

    def test_without_key_rebuilds_index_and_exits_zero(self):
        self.run_main()
        with patch.dict(bp.os.environ, {"GEMINI_API_KEY": ""}):
            self.write_risk(risk_doc(score=90))
            code, calls = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        index = json.loads((self.out_dir / "index.json").read_text())
        self.assertEqual(len(index["entries"]), 1, "previous briefing kept in the index")

    def test_dry_run_prints_prompt_and_writes_nothing(self):
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            code, calls = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertIn("DATA:", out.getvalue())
        self.assertIn('"risk_score_0_to_100": 61', out.getvalue())
        self.assertFalse(self.out_dir.exists())

    # ---- failure handling ----

    def test_failure_keeps_previous_briefing_and_exits_one(self):
        self.run_main()
        before = (self.out_dir / "vancouver_wildfire.json").read_text()
        self.write_risk(risk_doc(score=70))
        with patch("sys.stderr", new_callable=io.StringIO):
            code, _ = self.run_main(replies=[RuntimeError("HTTP 503")])
        self.assertEqual(code, 1)
        self.assertEqual((self.out_dir / "vancouver_wildfire.json").read_text(), before)
        index = json.loads((self.out_dir / "index.json").read_text())
        self.assertEqual(index["entries"][0]["generatedAt"], "2026-10-29T19:00:00Z")

    def test_invalid_model_output_is_rejected(self):
        bad = gemini_reply({"title": "", "situation": "x", "actions": [], "confidence": "medium"})
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            code, _ = self.run_main(replies=[bad])
        self.assertEqual(code, 1)
        self.assertFalse((self.out_dir / "vancouver_wildfire.json").exists())
        self.assertIn("FAILED", err.getvalue())

    def test_blocked_or_empty_reply_is_reported(self):
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            code, _ = self.run_main(replies=[{"promptFeedback": {"blockReason": "SAFETY"}}])
        self.assertEqual(code, 1)
        self.assertIn("SAFETY", err.getvalue())

    def test_post_json_retries_429_then_succeeds(self):
        responses = [
            urllib.error.HTTPError("u", 429, "rate", {}, io.BytesIO(b"slow down")),
            io.BytesIO(json.dumps({"ok": True}).encode()),
        ]

        class Resp:
            def __init__(self, data): self.data = data
            def __enter__(self): return self.data
            def __exit__(self, *a): return False

        def fake_open(req, timeout):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return Resp(r)

        with patch.object(bp.urllib.request, "urlopen", side_effect=fake_open), patch.object(bp.time, "sleep", lambda *_: None):
            self.assertEqual(bp.post_json("https://x", {}, {}), {"ok": True})

    def test_post_json_404_explains_model_setting(self):
        err = urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b"model not found"))
        with patch.object(bp.urllib.request, "urlopen", side_effect=err):
            with self.assertRaisesRegex(RuntimeError, "GEMINI_MODEL"):
                bp.post_json("https://x", {}, {})

    def test_model_override_env(self):
        with patch.dict(bp.os.environ, {"GEMINI_MODEL": "gemini-3.8-flash-lite"}):
            _, calls = self.run_main()
        self.assertEqual(len(calls), 1)
        self.assertIn("gemini-3.8-flash-lite:generateContent", calls[0]["url"])

    @patch.dict(bp.os.environ, {"GEMINI_MODEL": "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,gemini-flash-lite-latest"})
    def test_overloaded_primary_falls_back_to_next_model(self):
        busy = RuntimeError("Gemini request failed after 3 attempts: HTTP 503: high demand")
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            code, calls = self.run_main(replies=[busy, gemini_reply()])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("gemini-3.8-flash:generateContent", calls[0]["url"])
        self.assertIn("gemini-3.7-flash:generateContent", calls[1]["url"])
        self.assertIn("is busy; falling back", err.getvalue())
        index = json.loads((self.out_dir / "index.json").read_text())
        self.assertEqual(index["entries"][0]["model"], "gemini-3.7-flash")

    @patch.dict(bp.os.environ, {"GEMINI_MODEL": "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,gemini-flash-lite-latest"})
    def test_missing_model_falls_back_to_next_model(self):
        missing = RuntimeError("HTTP 404: model not found\nModel not found. Set GEMINI_MODEL ...")
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            code, calls = self.run_main(replies=[missing, gemini_reply()])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("not available to this key", err.getvalue())

    @patch.dict(bp.os.environ, {"GEMINI_MODEL": "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,gemini-flash-lite-latest"})
    def test_busy_then_missing_then_success_walks_the_chain(self):
        busy = RuntimeError("Gemini request failed after 3 attempts: HTTP 503: high demand")
        missing = RuntimeError("HTTP 404: model not found")
        with patch("sys.stderr", new_callable=io.StringIO):
            code, calls = self.run_main(replies=[busy, missing, gemini_reply()])
        self.assertEqual(code, 0)
        self.assertIn("gemini-3.5-flash:generateContent", calls[2]["url"])

    def test_non_capacity_error_does_not_fall_back(self):
        bad = RuntimeError("HTTP 400: invalid request")
        with patch("sys.stderr", new_callable=io.StringIO):
            code, calls = self.run_main(replies=[bad])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)

    @patch.dict(bp.os.environ, {"GEMINI_MODEL": "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,gemini-flash-lite-latest"})
    def test_all_models_busy_fails_and_names_each(self):
        busy = lambda: RuntimeError("Gemini request failed after 3 attempts: HTTP 503: high demand")
        n = len(bp.os.environ["GEMINI_MODEL"].split(","))
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            code, calls = self.run_main(replies=[busy() for _ in range(n)])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), n)
        for m in bp.os.environ["GEMINI_MODEL"].split(","):
            self.assertIn(f"{m}:", err.getvalue())

if __name__ == "__main__":
    unittest.main()
