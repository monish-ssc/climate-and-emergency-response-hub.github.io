"""
build_risk_index.py - list every risk file so the dashboard knows what exists and where.

Pipelines write to two places: flood_pipeline.py and wildfire_pipeline.py write
data/pipeline/risk/<region>_<hazard>.json, and hurricane_pipeline.py writes
data/samples/<region>_severe_storm.json. Run this after the pipelines; it reads every
<region>_<hazard>.json risk-response in both folders and writes data/pipeline/risk/index.json.
If the same region and hazard appear twice, the newest (by generatedAt) is listed.
"path" is relative to the data/ folder, so the dashboard loads `data/<path>`.

{
  "builtAt": "...",
  "entries": [
    {"regionId": "clinton", "regionLabel": "Clinton, BC", "hazard": "wildfire",
     "score": 18, "level": "low", "dataMode": "live",
     "generatedAt": "...", "expiresAt": "...", "path": "pipeline/risk/clinton_wildfire.json"}, ...
  ]
}

Standard library only.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
RISK_DIR = DATA_DIR / "pipeline" / "risk"
SEARCH_DIRS = [RISK_DIR, DATA_DIR / "samples"]


def main():
    found = {}
    for path in [p for d in SEARCH_DIRS for p in sorted(d.glob("*_*.json"))]:
        if path.name == "index.json":
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            a = doc["assessment"]
            entry = {
                "regionId": doc["region"]["id"],
                "regionLabel": doc["region"]["label"],
                "hazard": doc["hazard"],
                "score": a["score"],
                "level": a["level"],
                "dataMode": doc.get("dataMode"),
                "generatedAt": doc.get("generatedAt"),
                "expiresAt": doc.get("expiresAt"),
                "path": path.relative_to(DATA_DIR).as_posix(),
            }
        except (KeyError, ValueError, TypeError) as e:
            print(f"  skipped {path.name}: not a risk-response ({e})")
            continue
        key = (entry["regionId"], entry["hazard"])
        if key not in found or (entry["generatedAt"] or "") > (found[key]["generatedAt"] or ""):
            found[key] = entry
    entries = sorted(found.values(), key=lambda e: (e["regionId"], e["hazard"]))
    index = {
        "builtAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entries": entries,
    }
    RISK_DIR.mkdir(parents=True, exist_ok=True)
    (RISK_DIR / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    for e in entries:
        print(f"  {e['regionId']:<14} {e['hazard']:<13} {e['score']:>3} {e['level']:<9} {e['generatedAt']}  {e['path']}")
    print(f"Indexed {len(entries)} risk files -> {RISK_DIR / 'index.json'}")


if __name__ == "__main__":
    main()
