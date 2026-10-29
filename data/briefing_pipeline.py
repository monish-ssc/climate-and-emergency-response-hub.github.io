"""
briefing_pipeline.py - draft a preparedness briefing for every live risk file, using Gemini.

Runs after build_risk_index.py in the refresh workflow. For each entry in
data/pipeline/risk/index.json it:
  1. reads the risk-response and fingerprints the inputs that matter (score, level, factors,
     summary) so unchanged conditions do not call the model again;
  2. asks Gemini for a short briefing grounded only in that data, as JSON;
  3. validates the result against api/contracts/briefing-response.schema.json and writes
     data/pipeline/briefing/<regionId>_<hazard>.json, plus an index.json with the fingerprints.

The key is read from GEMINI_API_KEY (a GitHub Actions secret in CI). Without it the script
only rebuilds the index from existing files and exits 0, so local runs and forks still work.
A briefing that fails to generate keeps its last published copy; the exit code is 1 so the
workflow can report it, while continue-on-error keeps the deploy going.

  GEMINI_API_KEY=... python data/briefing_pipeline.py            # changed entries only
  GEMINI_API_KEY=... python data/briefing_pipeline.py --force    # regenerate everything
  python data/briefing_pipeline.py --dry-run                     # print prompts, no calls
  GEMINI_MODEL=gemini-3.8-flash-lite ...                         # one model, or a comma list to try in order
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
ROOT_DIR = DATA_DIR.parent
CONTRACTS_DIR = ROOT_DIR / "api" / "contracts"
RISK_INDEX = DATA_DIR / "pipeline" / "risk" / "index.json"
OUT_DIR = DATA_DIR / "pipeline" / "briefing"
OUT_INDEX = OUT_DIR / "index.json"

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.8-flash"  # GEMINI_MODEL may also be a comma list to try in order
MAX_AGE_HOURS = 24  # refresh the wording at least daily even if the numbers did not move
CALL_GAP_SECONDS = 1.0  # be gentle with free-tier rate limits
HAZARD_WORDS = {"wildfire": "wildfire", "flood": "flood", "severe_storm": "hurricane and severe storm"}
DISCLAIMER = (
    "AI-drafted from automated, provisional hazard screening. Not an official warning or "
    "forecast. A qualified person must review and approve before any operational use."
)

# What Gemini must return; the script adds the fixed contract fields itself.
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "required": ["title", "situation", "actions", "confidence"],
    "properties": {
        "title": {"type": "STRING"},
        "situation": {"type": "STRING"},
        "actions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "required": ["priority", "action", "rationale"],
                "properties": {
                    "priority": {"type": "INTEGER"},
                    "action": {"type": "STRING"},
                    "rationale": {"type": "STRING"},
                },
            },
        },
        "confidence": {"type": "STRING", "enum": ["low", "medium", "high"]},
    },
}


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------- change detection ----------


def fingerprint(risk):
    """Hash of the inputs that should change the briefing text. Timestamps are left out."""
    a = risk["assessment"]
    basis = {
        "hazard": risk["hazard"],
        "region": risk["region"]["id"],
        "score": a["score"],
        "level": a["level"],
        "summary": a["summary"],
        "factors": [
            [f["key"], f["value"], f["contribution"], f.get("explanation", "")]
            for f in a["factors"]
        ],
        "dataMode": risk.get("dataMode"),
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()[:24]


def needs_refresh(previous, fp, now, force):
    if force or previous is None:
        return True
    if previous.get("inputsFingerprint") != fp:
        return True
    try:
        age = now - parse_time(previous["generatedAt"])
    except (KeyError, ValueError):
        return True
    return age > timedelta(hours=MAX_AGE_HOURS)


# ---------- prompt + model call ----------


def prompt_for(risk):
    a = risk["assessment"]
    hazard = HAZARD_WORDS.get(risk["hazard"], risk["hazard"])
    facts = {
        "region": risk["region"]["label"],
        "hazard": hazard,
        "risk_score_0_to_100": a["score"],
        "risk_level": a["level"],
        "screening_summary": a["summary"],
        "factors": [
            {
                "what": f["label"],
                "value": f"{f['value']} {f['unit']}",
                "points": f["contribution"],
                "note": f.get("explanation", ""),
            }
            for f in a["factors"]
        ],
        "data_mode": risk.get("dataMode"),
        "data_generated_at_utc": risk.get("generatedAt"),
        "sources": [s["name"] for s in risk.get("sources", [])],
    }
    return (
        "You draft short preparedness briefings for Canadian emergency-management staff.\n"
        "Write one briefing from the DATA below and nothing else.\n\n"
        "Rules:\n"
        "- Use only numbers and facts that appear in DATA. Do not invent conditions, places, "
        "fires, storms, river names or forecasts.\n"
        "- The data is an automated prototype screening of provisional public feeds, not an "
        "official warning. Say so once, plainly.\n"
        "- 'title': under 80 characters, naming the region and hazard.\n"
        "- 'situation': 2 to 4 sentences in plain language summarising the current screening, "
        "including the score and level.\n"
        "- 'actions': 3 to 5 items, priority 1 = most urgent. Each 'action' is one concrete "
        "preparedness step proportional to the level (low = monitor and verify; high or "
        "critical = review plans, check official alerts, brief decision makers). Each "
        "'rationale' points at the specific factor in DATA that motivates it.\n"
        "- Always include an action to confirm against official ECCC / provincial alerts.\n"
        "- 'confidence': 'low' if data_mode is not 'live' or the factors say history or data "
        "is missing; otherwise 'medium'. Never 'high'.\n"
        "- Neutral, operational tone. No markdown, no emojis.\n\n"
        f"DATA:\n{json.dumps(facts, ensure_ascii=False, indent=1)}"
    )


def post_json(url, body, headers, timeout=60, attempts=3):
    data = json.dumps(body).encode("utf-8")
    error = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json",
            "User-Agent": "climate-hub-hackathon/0.1",
            **headers,
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}"
            if e.code == 404:
                raise RuntimeError(
                    f"{detail}\nModel not found. Set GEMINI_MODEL to a model id listed at "
                    "https://ai.google.dev/gemini-api/docs/models"
                ) from None
            if e.code < 500 and e.code != 429:
                raise RuntimeError(detail) from None
            error = detail
        except (urllib.error.URLError, TimeoutError) as e:
            error = f"{type(e).__name__}: {e}"
        if attempt < attempts - 1:
            wait = min(30, 2 ** attempt * 3)
            print(f"    attempt {attempt + 1}/{attempts} failed ({error[:120]}); retrying in {wait}s",
                  file=sys.stderr, flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Gemini request failed after {attempts} attempts: {error}")


def call_gemini(prompt, api_key, models):
    """Try each model in turn; move to the next only when one is overloaded or rate-limited."""
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }
    errors = []
    for i, model in enumerate(models):
        try:
            reply = post_json(GEMINI_URL.format(model=model), body, {"x-goog-api-key": api_key})
            return parse_reply(reply), model
        except RuntimeError as e:
            msg = str(e)
            busy = "failed after" in msg and ("HTTP 503" in msg or "HTTP 429" in msg or "HTTP 5" in msg)
            missing = "HTTP 404" in msg
            errors.append(f"{model}: {msg.splitlines()[0][:160]}")
            if not (busy or missing) or i == len(models) - 1:
                raise RuntimeError(" | ".join(errors)) from None
            why = "is busy" if busy else "is not available to this key"
            print(f"    {model} {why}; falling back to {models[i + 1]}", file=sys.stderr, flush=True)
    raise RuntimeError(" | ".join(errors))


def parse_reply(reply):
    try:
        candidate = reply["candidates"][0]
        text = "".join(p.get("text", "") for p in candidate["content"]["parts"])
    except (KeyError, IndexError, TypeError):
        blocked = reply.get("promptFeedback", {}).get("blockReason")
        raise RuntimeError(
            f"Gemini returned no candidate{f' (blocked: {blocked})' if blocked else ''}: "
            f"{json.dumps(reply)[:400]}"
        ) from None
    if candidate.get("finishReason") not in (None, "STOP"):
        raise RuntimeError(f"Gemini stopped early: {candidate.get('finishReason')}")
    try:
        return json.loads(text)
    except ValueError:
        raise RuntimeError(f"Gemini reply is not JSON: {text[:300]}") from None


# ---------- assemble + validate the contract document ----------


def citations_from(risk):
    seen, out = set(), []
    for s in risk.get("sources", []):
        url = s.get("url")
        if url and url not in seen:
            seen.add(url)
            out.append({"title": s["name"], "url": url})
    return out or [{"title": "ECCC MSC GeoMet", "url": "https://api.weather.gc.ca"}]


def build_document(draft, risk, now):
    actions = sorted(draft.get("actions", []), key=lambda x: x.get("priority", 99))[:7]
    for i, act in enumerate(actions, 1):
        act["priority"] = i  # normalise to 1..n, in case the model skipped or repeated
    return {
        "contractVersion": "1.0",
        "requestId": f"brief_{risk['region']['id']}_{risk['hazard']}_{uuid.uuid4().hex[:8]}",
        "title": str(draft.get("title", "")).strip(),
        "situation": str(draft.get("situation", "")).strip(),
        "actions": [
            {
                "priority": a["priority"],
                "action": str(a.get("action", "")).strip(),
                "rationale": str(a.get("rationale", "")).strip(),
            }
            for a in actions
        ],
        "confidence": "low" if draft.get("confidence") == "high" else draft.get("confidence", "low"),
        "requiresHumanReview": True,
        "citations": citations_from(risk),
        "generatedAt": iso(now),
        "disclaimer": DISCLAIMER,
    }


_validator = None


def validate(doc):
    global _validator
    if _validator is None:
        import jsonschema
        import referencing

        schemas = [read_json(p) for p in CONTRACTS_DIR.glob("*.schema.json")]
        registry = referencing.Registry().with_resources(
            (s["$id"], referencing.Resource.from_contents(s)) for s in schemas
        )
        schema = next(s for s in schemas if s["$id"].endswith("/briefing-response.schema.json"))
        _validator = jsonschema.Draft202012Validator(schema, registry=registry)
    _validator.validate(doc)


# ---------- main loop ----------


def load_previous_index():
    try:
        return {(e["regionId"], e["hazard"]): e for e in read_json(OUT_INDEX).get("entries", [])}
    except (OSError, ValueError, KeyError):
        return {}


def write_index(entries, model, now):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    doc = {
        "builtAt": iso(now),
        "model": model,
        "entries": sorted(entries, key=lambda e: (e["regionId"], e["hazard"])),
    }
    OUT_INDEX.write_text(json.dumps(doc, indent=1), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="regenerate every briefing")
    parser.add_argument("--dry-run", action="store_true", help="print prompts; do not call Gemini or write")
    parser.add_argument("--only", metavar="REGION_HAZARD", help="e.g. vancouver_wildfire")
    args = parser.parse_args(argv)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    models = [m.strip() for m in (os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL).split(",") if m.strip()]
    now = datetime.now(timezone.utc).replace(microsecond=0)

    risk_index = read_json(RISK_INDEX)
    previous = load_previous_index()
    entries, failed, generated, skipped = [], [], 0, 0

    for entry in risk_index.get("entries", []):
        key = (entry["regionId"], entry["hazard"])
        name = f"{entry['regionId']}_{entry['hazard']}"
        if args.only and args.only != name:
            continue
        out_path = OUT_DIR / f"{name}.json"
        prev = previous.get(key)
        if prev is not None and not out_path.exists():
            prev = None  # index says it exists but the file is gone: regenerate
        try:
            risk = read_json(DATA_DIR / entry["path"])
            fp = fingerprint(risk)
            if args.dry_run:
                print(f"\n===== {name} =====\n{prompt_for(risk)}")
                continue
            if not needs_refresh(prev, fp, now, args.force):
                entries.append(prev)
                skipped += 1
                continue
            if not api_key:
                if prev is not None:
                    entries.append(prev)
                continue
            print(f"  {name}: asking {models[0]} ...", flush=True)
            draft, used_model = call_gemini(prompt_for(risk), api_key, models)
            doc = build_document(draft, risk, now)
            validate(doc)
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
            entries.append({
                "regionId": entry["regionId"],
                "regionLabel": entry.get("regionLabel", risk["region"]["label"]),
                "hazard": entry["hazard"],
                "score": risk["assessment"]["score"],
                "level": risk["assessment"]["level"],
                "riskGeneratedAt": risk.get("generatedAt"),
                "generatedAt": doc["generatedAt"],
                "inputsFingerprint": fp,
                "model": used_model,
                "path": f"pipeline/briefing/{name}.json",
            })
            generated += 1
            print(f"  {name}: generated ({risk['assessment']['level']}, {risk['assessment']['score']})")
            time.sleep(CALL_GAP_SECONDS)
        except Exception as e:  # noqa: BLE001 - one bad entry must not block the others
            failed.append(name)
            print(f"  {name}: FAILED - {e}", file=sys.stderr)
            if prev is not None:
                entries.append(prev)  # keep the last good briefing on the site

    if args.dry_run:
        return 0
    write_index(entries, models[0], now)
    if not api_key:
        print(
            f"GEMINI_API_KEY not set: no briefings generated; index rebuilt with "
            f"{len(entries)} existing file(s). Set the key to enable drafting."
        )
        return 0
    print(f"\nBriefings: {generated} generated, {skipped} unchanged, {len(failed)} failed -> {OUT_DIR}")
    if failed:
        print(f"Failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
