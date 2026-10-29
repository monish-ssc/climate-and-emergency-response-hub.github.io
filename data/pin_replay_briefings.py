"""
pin_replay_briefings.py - save the replay AI briefings in the repo so the demo never depends on a live AI run.

The refresh workflow writes AI briefings for the replay moments into the published
data/pipeline/live/briefings.json. This script copies the accepted AI drafts (not templates) into
data/scenarios/<fireId>/briefings.json. Commit those files: build_briefings.py then uses them as they are
for the replays, and never redrafts them or falls back to a template.

Review step: read each saved draft, fix any wording by hand, and set "reviewed": true on the ones a
person has checked. The dashboard shows "reviewed by the team" on those.

Usage (from the repo root):
  python data/pin_replay_briefings.py                      # from the published site
  python data/pin_replay_briefings.py --file briefings.json   # from a downloaded copy
  python data/pin_replay_briefings.py --replace            # also overwrite drafts already saved

Without --replace, drafts already saved are kept (your edits and "reviewed" flags survive) and only
missing moments are added.
"""

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DATA_DIR))
import build_briefings as bb  # noqa: E402

DEFAULT_SITE = "https://monish-ssc.github.io/climate-and-emergency-response-hub.github.io"
KEEP = ("key", "kind", "fireId", "regionId", "hazard", "step", "score", "level", "asOf", "model", "generatedAt",
        "promptVersion", "hash", "briefing", "publicAlert", "facts")


def fetch(base_url):
    url = f"{base_url.rstrip('/')}/data/pipeline/live/briefings.json"
    print(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=DEFAULT_SITE, help=f"published site (default {DEFAULT_SITE})")
    ap.add_argument("--file", help="read a downloaded briefings.json instead of the site")
    ap.add_argument("--replace", action="store_true", help="overwrite drafts that are already saved")
    a = ap.parse_args()

    doc = json.loads(Path(a.file).read_text(encoding="utf-8")) if a.file else fetch(a.base_url)
    ai = {b["key"]: b for b in doc.get("briefings", [])
          if b.get("kind") == "replay" and b.get("model") not in (None, "template") and b.get("briefing")}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    missing_total = 0
    for rp in bb.load(DATA_DIR / "scenarios" / "index.json", {"replays": []})["replays"]:
        fire = rp["fireId"]
        tl = bb.load(DATA_DIR / "scenarios" / fire / "timeline.json")
        if not tl:
            continue
        steps = bb.replay_marks(tl["metadata"], tl["steps"])
        path = DATA_DIR / "scenarios" / fire / "briefings.json"
        saved = {b["key"]: b for b in (bb.load(path, {}) or {}).get("briefings", [])}
        added = kept = 0
        for i in steps:
            key = f"replay/{fire}/{i}"
            if key in saved and not a.replace:
                kept += 1
            elif key in ai:
                saved[key] = {**{k: ai[key].get(k) for k in KEEP}, "reviewed": False}
                added += 1
        missing = [i for i in steps if f"replay/{fire}/{i}" not in saved]
        missing_total += len(missing)
        out = {
            "note": "Saved AI briefings for this replay (pin_replay_briefings.py). Used as they are by "
                    "build_briefings.py. Edit the text if needed and set reviewed: true once a person has checked it.",
            "fireId": fire,
            "updatedAt": now,
            "source": a.file or a.base_url,
            "briefings": sorted(saved.values(), key=lambda b: b["step"]),
        }
        if saved:
            path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"{fire}: {len(saved)} of {len(steps)} moments saved ({added} added, {kept} kept)"
              + (f"; still template at steps {missing}" if missing else ""))
    if missing_total:
        print("Some moments have no AI draft yet. Run this again after the next refresh.")
    else:
        print("All replay moments are saved. Review the text, then commit data/scenarios/*/briefings.json.")


if __name__ == "__main__":
    main()
