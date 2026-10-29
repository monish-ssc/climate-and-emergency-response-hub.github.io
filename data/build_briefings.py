"""
build_briefings.py - AI preparedness briefings, grounded in the hub's own data.

Runs in the refresh workflow after the pipelines. For each subject it gathers numbered facts from the
files the pipelines wrote, asks a model on GitHub Models to write a briefing that uses only those facts,
checks the answer, and saves everything to data/pipeline/live/briefings.json for the dashboard.

Subjects
  live    every region x hazard in pipeline/risk/index.json (risk score and factors, smoke, disaster
          history, nearest hospitals, possible host communities, population)
  replay  every key moment of the wildfire replays (score changes and reported events): the fire, the
          replayed score, smoke, reception centres open at that time, what had really happened so far

Model: GitHub Models (https://models.github.ai), called with the workflow's GITHUB_TOKEN
(permissions: models: read), so no API key or secret is needed. Free use is rate-limited, so:
  - replay briefings never change once written; live ones are rewritten only when the score or level
    changes or after MAX_AGE_HOURS
  - at most --max-calls model calls per run, most urgent subjects first
  - previous briefings are reused from the published site (--base-url) or the local file

Guardrails
  - the model sees only the numbered facts and must cite them as [F1], [F2] ...
  - every number in its text must appear in the facts; otherwise it is retried once, then rejected
  - output follows api/contracts/briefing-response.schema.json; requiresHumanReview is always true
  - a subject without a valid AI briefing gets a template briefing built from the same facts
    (model "template"), so the dashboard always has something accurate to show

Run from the repo root:
  python data/build_briefings.py --base-url https://<site>     (in the workflow; GITHUB_TOKEN set)
  python data/build_briefings.py --offline                       (templates only, no model calls)
"""

import argparse
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
ROOT = DATA_DIR.parent
OUT = DATA_DIR / "pipeline" / "live" / "briefings.json"
ENDPOINT = "https://models.github.ai/inference/chat/completions"
MODELS = [m for m in os.environ.get("BRIEFING_MODELS", "openai/gpt-4.1-mini,openai/gpt-4o-mini").split(",") if m]
PROMPT_VERSION = "2026-10-29.1"
MAX_AGE_HOURS = 12
HAZARD_NAME = {"wildfire": "wildfire", "flood": "flood", "severe_storm": "hurricane and severe storm"}
HISTORY_KEY = {"wildfire": "wildfire", "flood": "flood", "severe_storm": "severe_storm"}
LEVEL_ORDER = {"critical": 0, "high": 1, "moderate": 2, "low": 3}
DISCLAIMER = ("AI-assisted draft from automated screening data. Not an official warning or order. "
              "A person must review it before any use.")

SYSTEM_PROMPT = """You write preparedness briefings for Canadian emergency managers.

Rules (follow all of them):
1. Use ONLY the numbered facts provided. Do not add facts, places, numbers, dates or organisations.
2. Cite the facts you use in square brackets, for example [F2] or [F2, F5], after each sentence.
3. Every number you write must appear in the facts, written the same way. If you need a number that is
   not in the facts, describe it in words instead.
4. Never say an evacuation, order or alert is in place unless a fact says so. The risk score is a
   screening tool, not an official warning.
5. Facts marked "assumption" must be called an assumption if you use them.
6. Match the actions to the risk level: low = stay ready and monitor; moderate = review plans and
   resources; high = prepare to act (reception centres, transport, vulnerable people); critical =
   coordinate now with the local emergency operations centre.
7. Write plain, calm language. Canadian spelling. The French text is Canadian French.

Reply with JSON only, in exactly this shape:
{"title": "...", "situation": "2-4 sentences with citations",
 "actions": [{"priority": 1, "action": "...", "rationale": "... [F#]"}],
 "confidence": "low|medium|high",
 "publicAlert": {"en": {"headline": "...", "body": "...", "whatToDo": ["...", "..."]},
                 "fr": {"headline": "...", "body": "...", "whatToDo": ["...", "..."]}}}
Give 3 to 5 actions. Confidence: high if the facts are recent and consistent, low if key data is
missing or stale. The public alert is for residents, has no citations and no numbers that are not in
the facts, and must not claim an official order. Do not calculate new numbers and do not add numbers
from outside the facts (road numbers, phone numbers, counts); copy numbers exactly as the facts give them."""


# ---------- helpers ----------

def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(a))


def n(x, digits=0):
    """Numbers written the way the facts (and so the model) should write them."""
    if x is None:
        return "unknown"
    if digits == 0:
        return f"{round(x):,}"
    return f"{x:,.{digits}f}"


def proper(s):
    return re.sub(r"(^|[\s\-(/])(\w)", lambda m: m.group(1) + m.group(2).upper(), str(s or "").lower())


class Facts:
    def __init__(self):
        self.items, self.sources = [], {}

    def add(self, text, source=None, url=None):
        self.items.append(text)
        if source:
            self.sources.setdefault(source, url or "")
        return f"F{len(self.items)}"

    def text(self):
        return "\n".join(f"[F{i}] {t}" for i, t in enumerate(self.items, 1))

    def citations(self):
        return [{"title": t, "url": u or "https://climate-and-emergency-response-hub.github.io/"}
                for t, u in self.sources.items()]


# ---------- shared reference data ----------

class Reference:
    def __init__(self):
        self.regions = {r["id"]: r for r in load(DATA_DIR / "scenarios.json")["regions"]}
        pop = load(DATA_DIR / "pipeline" / "population_summary.json", {"communities": [], "metadata": {}})
        self.pop_year = pop["metadata"].get("year")
        self.communities = pop["communities"]
        self.pop_by_csd = {c.get("csduid"): c for c in self.communities}
        self.history = load(DATA_DIR / "history" / "disaster_history.json", {"regions": {}})
        fac = load(DATA_DIR / "healthcare" / "facilities.geojson", {"features": []})
        self.hospitals = [f for f in fac["features"] if (f.get("properties") or {}).get("type") == "Hospitals"
                          and f.get("geometry")]

    def population(self, region):
        return self.pop_by_csd.get(region.get("csduid"))

    def nearest_hospitals(self, lat, lon, k=3):
        seen, out = set(), []
        for f in sorted(self.hospitals, key=lambda f: km(lat, lon, f["geometry"]["coordinates"][1],
                                                          f["geometry"]["coordinates"][0])):
            lo, la = f["geometry"]["coordinates"][:2]
            key = (round(la, 2), round(lo, 2))
            if key in seen:
                continue
            seen.add(key)
            out.append((proper(f["properties"]["name"]), proper(f["properties"].get("municipality")), km(lat, lon, la, lo)))
            if len(out) == k:
                break
        return out

    def hosts(self, region, k=3):
        own = region.get("csduid")
        cands = [(c, km(region["lat"], region["lon"], c["centre_lat"], c["centre_lon"]))
                 for c in self.communities
                 if c.get("csduid") != own and (c.get("population") or 0) >= 10000 and c.get("centre_lat") is not None]
        return sorted([x for x in cands if 30 <= x[1] <= 250], key=lambda x: x[1])[:k]


def add_context(facts, ref, region, hazard):
    """Population, history, hospitals and host communities: the planning side of every briefing.
    Returns {name: (fact id, data)} so template actions can name and cite them."""
    ctx = {}
    pop = ref.population(region)
    if pop and pop.get("population"):
        facts.add(f"{pop['name']} has about {n(pop['population'])} residents (Statistics Canada {ref.pop_year} estimate).",
                  "Statistics Canada population estimates", "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=1710015501")
    h = (ref.history.get("regions", {}).get(region["id"], {}).get("hazards", {}) or {}).get(HISTORY_KEY.get(hazard, hazard))
    cdd = ("Canadian Disaster Database", "https://www.publicsafety.gc.ca/cnt/rsrcs/cndn-dsstr-dtbs/index-en.aspx")
    if h:
        base = h.get("evacuationBaseline") or {}
        if base.get("median") is not None:
            facts.add(f"In past {HAZARD_NAME[hazard]} disasters in {h['province']} with evacuations "
                      f"({base['basedOnEvents']} events, {base['window']}), the median number of people evacuated was "
                      f"{n(base['median'])}; one in four evacuated {n(base['p75'])} or more.", *cdd)
        for e in [e for e in h.get("similarEvents", []) if e.get("evacuated")][:2]:
            facts.add(f"Similar past event: {e['startDate'][:4]}, {e['place']}: {n(e['evacuated'])} people evacuated.", *cdd)
    hosp = ref.nearest_hospitals(region["lat"], region["lon"])
    if hosp:
        ctx["hospitals"] = (f"F{len(facts.items) + 1}", hosp)
        facts.add("Nearest hospitals in the 2020 Open Database of Healthcare Facilities (capacity not verified): "
                  + "; ".join(f"{nm}, {town} ({n(d, 1)} km)" for nm, town, d in hosp) + ".",
                  "Open Database of Healthcare Facilities (Statistics Canada)", "https://www.statcan.gc.ca/en/lode/databases/odhf")
    hosts = ref.hosts(region)
    if hosts:
        ctx["hosts"] = (f"F{len(facts.items) + 1}", hosts)
        facts.add("Possible host communities for evacuees (10,000+ residents, 30-250 km away; not confirmed reception sites): "
                  + "; ".join(f"{c['name']} ({n(c['population'])} residents, {n(d)} km)" for c, d in hosts) + ".")
    lodging = "about 20% of evacuees"
    if pop and pop.get("population"):
        lodging += f", about {n(round(pop['population'] * 0.2))} people if all {n(pop['population'])} residents leave"
    facts.add(f"Assumption: plan group lodging for {lodging} (an adjustable planning assumption, not data).")
    return ctx


# ---------- subjects ----------

def live_subjects(ref):
    index = load(DATA_DIR / "pipeline" / "risk" / "index.json", {"entries": []})
    out = []
    for e in index["entries"]:
        region = ref.regions.get(e["regionId"])
        doc = load(DATA_DIR / e["path"])
        if not region or not doc:
            continue
        a = doc["assessment"]
        facts = Facts()
        facts.add(f"Place: {region['label']}. Hazard: {HAZARD_NAME[e['hazard']]}.")
        score_fact = f"F{len(facts.items) + 1}"
        facts.add(f"Automated {HAZARD_NAME[e['hazard']]} risk score: {a['score']} out of 100, level {a['level']} "
                  f"(screening score, not an official warning). Data generated {doc['generatedAt']}.")
        if a.get("summary"):
            facts.add(f"Assessment summary: {a['summary']}")
        for f in sorted(a.get("factors", []), key=lambda f: -(f.get("contribution") or 0))[:4]:
            facts.add(f"Score factor - {f['label']}: {f['value']} {f.get('unit') or ''} "
                      f"({n(f.get('contribution'), 1)} points). {f.get('explanation') or ''}".strip())
        for s in doc.get("sources", []):
            facts.sources.setdefault(s["name"], s.get("url", ""))
        aq = load(DATA_DIR / "pipeline" / "air_quality" / f"{region['id']}_aqhi.json")
        if aq and aq.get("current") and aq.get("station"):
            facts.add(f"Air quality (AQHI) at {aq['station']['name']}, {n(aq['station']['distanceKm'])} km away: "
                      f"{aq['current']['display']} ({aq['current']['category']} health risk); forecast peak "
                      f"{(aq.get('forecastPeak') or {}).get('display', 'unknown')}.",
                      "ECCC Air Quality Health Index", "https://api.weather.gc.ca/collections/aqhi-observations-realtime")
        ctx = add_context(facts, ref, region, e["hazard"])
        ctx["score"] = (score_fact, None)
        out.append({
            "ctx": ctx,
            "key": f"live/{region['id']}/{e['hazard']}",
            "kind": "live",
            "regionId": region["id"],
            "hazard": e["hazard"],
            "score": a["score"],
            "level": a["level"],
            "asOf": doc["generatedAt"],
            "facts": facts,
        })
    return out


WATCH_MAX = 8  # briefings for the top places to watch (moderate or above)


def watch_subjects(ref):
    """The top 'places to watch': one briefing per fire or river station, planned around the most
    affected community. Refreshed only when the level changes or the draft is old (see needs_refresh)."""
    doc = load(DATA_DIR / "pipeline" / "live" / "watchlist.json")
    if not doc:
        return []
    out = []
    for g in [g for g in doc.get("hotspots", []) if g.get("level") != "low" and g.get("id")][:WATCH_MAX]:
        lead = g["communities"][0]
        noun = HAZARD_NAME[g["hazard"]]
        facts = Facts()
        facts.add(f"Place: {g['label']} area. Hazard: {noun}. Found by screening every community of 1,000+ "
                  "people in Canada.")
        score_fact = f"F{len(facts.items) + 1}"
        facts.add(f"Highest automated {noun} screening score among nearby communities: {g['score']} out of 100, "
                  f"level {g['level']} (screening only, not an official warning; weather alerts and fire history "
                  f"are not included). Data generated {doc['generatedAt']}.")
        if g["hazard"] == "wildfire":
            facts.add(f"{g['label']}: {g['detail']}.", "CWFIS active fires (NRCan)", "https://cwfis.cfs.nrcan.gc.ca/")
        else:
            facts.add(f"River station {g['label']} ({g['id'].removeprefix('river-')}): {g['detail']}.",
                      "ECCC real-time hydrometric data", "https://wateroffice.ec.gc.ca/")
        dist = lambda c: (f", {n(km(c['lat'], c['lon'], g['lat'], g['lon']))} km away"  # noqa: E731
                          if g.get("lat") is not None and c.get("lat") is not None else "")
        facts.add(f"Communities above low ({g['communityCount']}, about {n(g['population'])} people in total): "
                  + "; ".join(f"{c['name']}, {c.get('province') or ''} ({n(c['population'])} residents, score "
                              f"{c['score']}{dist(c)})" for c in g["communities"][:5]) + ".")
        region = {"id": f"watch-{g['id']}", "label": f"{lead['name']}, {lead.get('province') or ''}".strip(", "),
                  "lat": lead["lat"], "lon": lead["lon"], "csduid": lead.get("csduid")}
        ctx = add_context(facts, ref, region, g["hazard"])
        ctx["score"] = (score_fact, None)
        out.append({
            "ctx": ctx,
            "key": f"watch/{g['id']}",
            "kind": "watch",
            "watchId": g["id"],
            "regionId": region["id"],
            "hazard": g["hazard"],
            "score": g["score"],
            "level": g["level"],
            "asOf": doc["generatedAt"],
            "facts": facts,
        })
    return out


def replay_marks(meta, steps):
    """Steps worth a briefing: score changes and reported events (same rule as the dashboard)."""
    idx = set()
    for k in meta.get("keyMoments", [])[1:]:
        for i, s in enumerate(steps):
            if s["t"] == k["t"]:
                idx.add(i)
    for ev in meta.get("referenceEvents", []):
        t = datetime.fromisoformat(f"{ev['date']}T{ev.get('timeLocal') or '12:00'}:00-07:00")
        i = next((i for i, s in enumerate(steps) if datetime.fromisoformat(s["t"].replace("Z", "+00:00")) >= t), len(steps) - 1)
        idx.add(i)
    return sorted(idx)


def replay_subjects(ref):
    index = load(DATA_DIR / "scenarios" / "index.json", {"replays": []})
    out = []
    for rp in index["replays"]:
        folder = DATA_DIR / "scenarios" / rp["fireId"]
        tl = load(folder / "timeline.json")
        if not tl:
            continue
        aq, res = load(folder / "air_quality.json"), load(folder / "resources.json")
        steps, meta = tl["steps"], tl["metadata"]
        region = ref.regions.get(meta["region"]["id"], meta["region"])
        fire_name = re.sub(r"\s*\(.*\)\s*$", "", meta["label"].replace("Replay: ", ""))
        for i in replay_marks(meta, steps):
            st = steps[i]
            a, fire = st["risk"]["assessment"], st.get("fire") or {}
            t = datetime.fromisoformat(st["t"].replace("Z", "+00:00"))
            lt = t.astimezone(timezone(timedelta(hours=-7)))
            local = f"{lt:%b} {lt.day}, {lt.year}, {lt.hour % 12 or 12} {lt:%p} PDT"  # no %-d: not on Windows
            facts = Facts()
            facts.add(f"Historical replay of a real event. Time: {local}. Place: {region['label']}.")
            facts.add(f"{fire_name} ({fire.get('agencyFireId')}): {fire.get('stageLabel')}, about {n(fire.get('sizeHa'))} ha"
                      f"{'' if st.get('isReported') else ' (estimated between agency reports)'}; its approximate edge is "
                      f"{n(fire.get('edgeDistanceKm'), 1)} km from {region['label']}.",
                      "CWFIS reported fires (NRCan)", "https://cwfis.cfs.nrcan.gc.ca/")
            score_fact = f"F{len(facts.items) + 1}"
            facts.add(f"Replayed wildfire risk score: {a['score']} out of 100, level {a['level']} "
                      "(computed only from information available at that time).")
            for f in sorted(a.get("factors", []), key=lambda f: -(f.get("contribution") or 0))[:3]:
                facts.add(f"Score factor - {f['label']}: {f['value']} {f.get('unit') or ''} ({n(f.get('contribution'), 1)} points).")
            if aq:
                row = next((x for x in aq["steps"] if x["t"] == st["t"]), None)
                if row and row.get("aqhiPlus") is not None:
                    stn = next((s for s in aq["stations"] if any(x["emsId"] == s["ems_id"] and x["pm25Max"] is not None
                                                                  for x in row["stations"])), aq["stations"][0])
                    facts.add(f"Smoke: AQHI+ {row['display']} ({row['category']} health risk) at {stn['name']}, "
                              f"{n(stn['distanceKm'])} km away.", "BC Ministry of Environment air quality data",
                              "https://www2.gov.bc.ca/gov/content/environment/air-land-water/air")
            ctx_centres = None
            if res:
                open_now = [c for c in res.get("receptionCentres", [])
                            if c.get("openedAt") and c.get("opensAtStep") is not None and i >= c["opensAtStep"]
                            and not (c.get("closedAt") and t > datetime.fromisoformat(c["closedAt"].replace("Z", "+00:00")))]
                ctx_centres = (f"F{len(facts.items) + 1}", open_now)
                facts.add("Reception centres open at this time: " + ("; ".join(
                    f"{c['name']}, {c['city']} ({n(c.get('distanceFromRegionKm'))} km)" for c in open_now) or "none") + ".",
                    "Reception centre announcements (EmergencyInfoBC, local news)", (open_now[0]["source"] if open_now else ""))
            happened = [ev for ev in meta.get("referenceEvents", [])
                        if datetime.fromisoformat(f"{ev['date']}T{ev.get('timeLocal') or '12:00'}:00-07:00") <= t]
            ctx_events = []
            for ev in happened[-2:]:
                ctx_events.append((f"F{len(facts.items) + 1}", ev))
                facts.add(f"Reported on {ev['date']}: {ev['event']}.", "News and official reports at the time", ev.get("source"))
            ctx = add_context(facts, ref, region, "wildfire")
            if res:
                ctx["centres"] = ctx_centres
            ctx["events"] = ctx_events
            ctx["score"] = (score_fact, None)
            out.append({
                "ctx": ctx,
                "key": f"replay/{rp['fireId']}/{i}",
                "kind": "replay",
                "fireId": rp["fireId"],
                "regionId": region["id"],
                "hazard": "wildfire",
                "step": i,
                "score": a["score"],
                "level": a["level"],
                "asOf": st["t"],
                "facts": facts,
            })
    return out


# ---------- model call and checks ----------

JSON_MODE = {"on": True}  # turned off if a model rejects response_format


def parse_json(text):
    """The model's JSON, tolerating code fences or text around it."""
    text = text.strip()
    if not text.startswith("{"):
        a, b = text.find("{"), text.rfind("}")
        if a < 0 or b < a:
            raise ValueError("no JSON object in the reply")
        text = text[a:b + 1]
    return json.loads(text)


def call_model(token, model, facts_text, feedback=None):
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Facts:\n" + facts_text}]
    if feedback:
        messages.append({"role": "user", "content": feedback})
    payload = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 1400}
    if JSON_MODE["on"]:
        payload["response_format"] = {"type": "json_object"}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=90) as r:
        raw, status, ctype = r.read().decode("utf-8", "replace"), r.status, r.headers.get("Content-Type")
    try:
        out = json.loads(raw)
    except ValueError:
        # show in the workflow log what came back instead of JSON (empty body, HTML page, ...)
        print(f"  model reply was not JSON: HTTP {status}, {ctype}, {len(raw)} bytes: {raw[:300]!r}")
        raise ValueError(f"reply was not JSON (HTTP {status}, {len(raw)} bytes)") from None
    return parse_json(out["choices"][0]["message"]["content"])


NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


CLOCK = re.compile(r"\b\d{1,2}(?::\d{2}|\s?h\s?\d{2})\b")  # 1:00, 13 h 00 - times, not quantities


# common planning time windows ("within 48 hours", "72-hour kit") are wording, not data
WINDOW = re.compile(r"\b(?:12|24|36|48|72)[\s\u00a0-]*(?:h\b|hours?\b|heures?\b)", re.I)


def numbers(text, french=False):
    text = WINDOW.sub(" ", CLOCK.sub(" ", text or ""))
    if french:
        # French Canadian: "1 684" and "17,3" are 1684 and 17.3
        text = re.sub(r"(?<=\d)[\s\u00a0\u202f](?=\d{3}\b)", "", text)
        text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    return {m.group(0).replace(",", "").rstrip(".") for m in NUM.finditer(text)}


# Actions that only fit a high or critical risk: opening or staffing reception centres, evacuating,
# relocating people. A low or moderate briefing must not recommend them (rule 6 of the prompt).
STRONG_ACTION = re.compile(
    r"\b(open|opening|activate|activating|staff|staffing|set up)\b[^.]{0,40}\b(reception|evacuation|shelter|emergency operations)"
    r"|\bevacuat(e|ing|ion order)\b|\brelocat(e|ing)\b|\bprepare\b[^.]{0,30}\breception centres?\b",
    re.I)


def check(answer, facts, level=None):
    """Problems with an answer, or [] if it is usable."""
    problems = []
    for k in ("title", "situation", "actions", "confidence", "publicAlert"):
        if k not in answer:
            problems.append(f"missing '{k}'")
    if problems:
        return problems
    if answer["confidence"] not in ("low", "medium", "high"):
        problems.append("confidence must be low, medium or high")
    acts = answer["actions"]
    if not isinstance(acts, list) or not 1 <= len(acts) <= 7 or not all(
            isinstance(a, dict) and a.get("action") and a.get("rationale") for a in acts):
        problems.append("actions must be 1-7 objects with action and rationale")
    if level in ("low", "moderate") and isinstance(acts, list):
        strong = [a.get("action", "") for a in acts if isinstance(a, dict) and STRONG_ACTION.search(a.get("action", ""))]
        if strong:
            problems.append(f"the risk level is {level}, so do not recommend opening reception centres, "
                            f"evacuating or relocating people (found: {strong[0][:60]}); recommend readiness and "
                            "planning steps instead")
    allowed = numbers(facts.text()) | {str(i) for i in range(0, 11)}
    written = " ".join([answer["title"], answer["situation"]] +
                       [f"{a.get('action', '')} {re.sub(r'\[F[0-9, F]+\]', '', a.get('rationale', ''))}" for a in acts if isinstance(a, dict)])
    alert = answer.get("publicAlert") or {}
    french = ""
    for lang in ("en", "fr"):
        p = alert.get(lang) or {}
        if not p.get("headline") or not p.get("body"):
            problems.append(f"publicAlert.{lang} needs headline and body")
        text = " " + " ".join([p.get("headline", ""), p.get("body", "")] + list(p.get("whatToDo") or []))
        if lang == "fr":
            french += text
        else:
            written += text
    cites = re.compile(r"\[F[0-9, F\-]+\]")
    invented = sorted((numbers(cites.sub("", written)) | numbers(cites.sub("", french), french=True)) - allowed)
    if invented:
        problems.append("numbers not in the facts: " + ", ".join(invented[:8]))
    if not re.search(r"\[F\d+", answer["situation"]):
        problems.append("the situation must cite facts like [F2]")
    return problems


def contract(answer, subject, model, now):
    acts = sorted(answer["actions"], key=lambda a: a.get("priority") or 99)[:7]
    return {
        "contractVersion": "1.0",
        "requestId": str(uuid.uuid5(uuid.NAMESPACE_URL, subject["key"] + subject["hash"])),
        "title": answer["title"].strip(),
        "situation": answer["situation"].strip(),
        "actions": [{"priority": i, "action": a["action"].strip(), "rationale": a["rationale"].strip()}
                    for i, a in enumerate(acts, 1)],
        "confidence": answer["confidence"],
        "requiresHumanReview": True,
        "citations": subject["facts"].citations() or [{"title": "ClimateGuard data files", "url": "https://github.com/monish-ssc/climate-and-emergency-response-hub.github.io"}],
        "generatedAt": iso(now),
        "disclaimer": DISCLAIMER,
    }


# ---------- template fallback (no model) ----------

def day(iso_date):
    """2026-07-24 -> Jul 24 (works on Windows too)."""
    d = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{d.strftime('%b')} {d.day}"


def template_actions(subject):
    """Level-appropriate actions that name and cite what the facts contain (centres, hospitals, hosts, events)."""
    level, c = subject["level"], subject.get("ctx") or {}
    cite = lambda key: f" [{c[key][0]}]" if c.get(key) else ""  # noqa: E731
    hosp = c["hospitals"][1][0] if c.get("hospitals") else None
    host = c["hosts"][1][0][0]["name"] if c.get("hosts") else None
    centres = (c.get("centres") or (None, []))[1] or []
    events = c.get("events") or []
    evac = next(((fid, ev) for fid, ev in reversed(events) if re.search(r"evacuat", ev["event"], re.I)
                 and not re.search(r"downgrad|all clear|rescind|lifted", ev["event"], re.I)), None)
    easing = next(((fid, ev) for fid, ev in reversed(events)
                   if re.search(r"downgrad|all clear|rescind|lifted|return", ev["event"], re.I)), None)
    sc = c["score"][0] if c.get("score") else "F2"
    acts = []
    if subject.get("kind") == "watch" and level in ("critical", "high", "moderate"):
        # a screening result, not a confirmed event: verify first, prepare at most
        acts.append(("Confirm the situation with local officials and check official alerts before acting.",
                     f"This is an automated screening score, not a warning [{sc}]."))
        level = "high" if level == "critical" else level
    if level == "critical":
        acts.append(("Coordinate the response with the local emergency operations centre.", f"The score is critical [{sc}]."))
        if easing:
            acts.append((f"Plan the return of residents with local officials (reported {day(easing[1]['date'])}).",
                         f"Orders are easing [{easing[0]}]."))
        if centres:
            ce = centres[0]
            acts.append((f"Keep {ce['name']} ({ce['city']}) staffed for evacuees and confirm it can take more people.",
                         f"It is the reception centre open now{cite('centres')}."))
        elif host:
            acts.append((f"Open a reception centre in a host community such as {host}.", f"No reception centre is open yet{cite('hosts')}."))
        if evac and not easing:
            acts.append((f"Support residents evacuated after the {day(evac[1]['date'])} report: registration, lodging and transport.",
                         f"An evacuation was reported [{evac[0]}]."))
        elif not evac and not easing:
            acts.append(("Arrange transport for residents who cannot leave on their own.", "They need the most lead time."))
        if hosp:
            acts.append((f"Confirm patient-transfer and care-home relocation plans with {hosp[0]} ({hosp[1]}).",
                         f"It is the nearest hospital in the inventory{cite('hospitals')}."))
    elif level == "high":
        if centres:
            ce = centres[0]
            acts.append((f"Keep {ce['name']} ({ce['city']}) staffed and ready for more evacuees.",
                         f"The score is {subject['level']} [{sc}] and this reception centre is open{cite('centres')}."))
        else:
            acts.append(("Prepare reception centres and transport so they can open quickly.", f"The score is {subject['level']} [{sc}]."))
        if evac:
            acts.append((f"Support residents evacuated after the {day(evac[1]['date'])} report: registration, lodging and transport.",
                         f"An evacuation was reported [{evac[0]}]."))
        if host:
            acts.append((f"Confirm {host} as a possible host community for evacuees.", f"Largest nearby option{cite('hosts')}."))
        if hosp:
            acts.append((f"Contact {hosp[0]} and local care homes about relocation plans.",
                         f"Vulnerable residents need the most lead time{cite('hospitals')}."))
    elif level == "moderate":
        acts.append(("Review the local emergency plan for this hazard.", f"The score is moderate [{sc}]."))
        if host:
            acts.append((f"Check reception-site and transport options with {host}.", f"Possible host community{cite('hosts')}."))
        acts.append(("Identify residents who would need help to leave.", "Early lists save time if the risk rises."))
    else:
        acts.append(("Keep monitoring official alerts and the hub's data.", f"The score is low [{sc}]."))
        acts.append(("Confirm contact lists and reception-site options are current.", "Readiness costs little while risk is low."))
    return acts[:5]


def template(subject, now):
    facts, level = subject["facts"], subject["level"]
    region_line = facts.items[0]
    title = f"{level.capitalize()} {HAZARD_NAME[subject['hazard']]} risk - " + region_line.split("Place: ")[-1].split(".")[0]
    situation = " ".join(f"{t} [F{i}]" for i, t in enumerate(facts.items[1:3], 2))
    answer = {
        "title": title,
        "situation": situation,
        "actions": [{"priority": i, "action": a, "rationale": r} for i, (a, r) in enumerate(template_actions(subject), 1)],
        "confidence": "medium",
    }
    out = contract(answer, subject, "template", now)
    out["confidence"] = "medium"
    return out, {
        "en": {"headline": f"{HAZARD_NAME[subject['hazard']].capitalize()} risk: {level}",
               "body": "Automated screening shows this risk level. Follow official alerts from your local authority.",
               "whatToDo": ["Check official alerts.", "Keep an emergency kit ready."]},
        "fr": {"headline": f"Risque ({HAZARD_NAME[subject['hazard']]}) : {level}",
               "body": "Le dépistage automatisé indique ce niveau de risque. Suivez les alertes officielles de votre autorité locale.",
               "whatToDo": ["Consultez les alertes officielles.", "Gardez une trousse d'urgence prête."]},
    }


# ---------- main ----------

def previous(base_url):
    prev = load(OUT, None)
    if base_url:
        try:
            with urllib.request.urlopen(f"{base_url.rstrip('/')}/data/pipeline/live/briefings.json", timeout=30) as r:
                prev = json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001 - first run, or the site is unreachable
            print(f"No published briefings reused ({e})")
    return {b["key"]: b for b in (prev or {}).get("briefings", [])}


def needs_refresh(subject, old, now):
    if not old or old.get("model") == "template" or old.get("promptVersion") != PROMPT_VERSION:
        return True
    if subject["kind"] == "replay":
        return old.get("hash") != subject["hash"]
    if subject["kind"] == "watch":  # screening scores move every run: redraft on a level change only
        if old.get("level") != subject["level"]:
            return True
    elif old.get("score") != subject["score"] or old.get("level") != subject["level"]:
        return True
    age = now - datetime.fromisoformat(old["generatedAt"].replace("Z", "+00:00"))
    return age > timedelta(hours=MAX_AGE_HOURS)


def build(base_url=None, max_calls=8, offline=False, now=None):
    now = now or datetime.now(timezone.utc)
    ref = Reference()
    subjects = replay_subjects(ref) + live_subjects(ref) + watch_subjects(ref)
    for s in subjects:
        s["hash"] = hashlib.sha256((PROMPT_VERSION + s["facts"].text()).encode()).hexdigest()[:16]
    old = previous(base_url)
    token = os.environ.get("GITHUB_TOKEN")
    if not token and not offline:
        print("GITHUB_TOKEN not set: writing template briefings only")
        offline = True

    # most urgent first: replays (written once), then live by level. Subjects whose last AI answer
    # failed the checks go to the back, so they cannot use up every run's calls while the rest wait.
    todo = [s for s in subjects if needs_refresh(s, old.get(s["key"]), now)]
    todo.sort(key=lambda s: (s["kind"] != "replay", bool((old.get(s["key"]) or {}).get("aiRejected")),
                             LEVEL_ORDER.get(s["level"], 9), s["key"]))
    calls, written, failed, models = 0, 0, 0, list(MODELS)
    results = {}
    for s in todo:
        if offline or calls >= max_calls or not models:
            break
        answer, problems = None, ["not called"]
        for attempt in range(2):
            try:
                calls += 1
                answer = call_model(token, models[0], s["facts"].text(),
                                    None if attempt == 0 else "Your previous answer had these problems: "
                                    + "; ".join(problems) + ". Fix them and reply with the full JSON again.")
                problems = check(answer, s["facts"], s["level"])
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:200]
                print(f"  {s['key']}: {models[0]} HTTP {e.code} {detail}")
                if e.code == 400 and JSON_MODE["on"] and "response_format" in detail:
                    JSON_MODE["on"] = False  # this model has no JSON mode; ask for JSON in the prompt only
                    problems = ["retrying without JSON mode"]
                    continue
                if e.code in (401, 403):
                    models = []  # token cannot use GitHub Models: stop calling
                elif e.code in (404, 422, 429):
                    models.pop(0)  # unknown model or its daily limit: try the next model
                problems = [f"HTTP {e.code}"]
                break
            except Exception as e:  # noqa: BLE001
                problems = [str(e)[:120]]
            if not problems or calls >= max_calls:
                break
            time.sleep(1)
        if answer and not problems:
            results[s["key"]] = {"briefing": contract(answer, s, models[0] if models else "unknown", now),
                                 "publicAlert": answer["publicAlert"], "model": models[0] if models else "unknown"}
            written += 1
            print(f"  AI  {s['key']} ({s['level']})")
        else:
            failed += 1
            results[s["key"]] = {"problems": problems}
            print(f"  --  {s['key']}: {'; '.join(problems)}")
        time.sleep(1)

    out = []
    for s in subjects:
        prev = old.get(s["key"])
        if s["key"] in results and "briefing" in results[s["key"]]:
            r = results[s["key"]]
            entry = {"briefing": r["briefing"], "publicAlert": r["publicAlert"], "model": r["model"],
                     "generatedAt": r["briefing"]["generatedAt"]}
        elif prev and prev.get("model") != "template" and prev.get("promptVersion") == PROMPT_VERSION and (
                s["kind"] in ("live", "watch") or prev.get("hash") == s["hash"]):
            entry = {k: prev[k] for k in ("briefing", "publicAlert", "model", "generatedAt")}
            entry["stale"] = needs_refresh(s, prev, now)
        else:
            b, alert = template(s, now)
            entry = {"briefing": b, "publicAlert": alert, "model": "template", "generatedAt": iso(now)}
            if s["key"] in results:  # the model was asked but its answer failed the checks
                entry["aiRejected"] = results[s["key"]]["problems"]
        entry.update({"key": s["key"], "kind": s["kind"], "regionId": s["regionId"], "hazard": s["hazard"],
                      "step": s.get("step"), "fireId": s.get("fireId"), "watchId": s.get("watchId"), "score": s["score"], "level": s["level"],
                      "asOf": s["asOf"], "hash": s["hash"], "promptVersion": PROMPT_VERSION,
                      "facts": s["facts"].items})
        out.append(entry)

    doc = {"generatedAt": iso(now), "endpoint": "GitHub Models", "models": MODELS, "promptVersion": PROMPT_VERSION,
           "counts": {"subjects": len(subjects), "ai": sum(e["model"] != "template" for e in out),
                      "template": sum(e["model"] == "template" for e in out), "calls": calls,
                      "written": written, "failed": failed},
           "briefings": out}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc


def main():
    ap = argparse.ArgumentParser(description="Write grounded AI briefings with GitHub Models.")
    ap.add_argument("--base-url", default=os.environ.get("PAGES_BASE_URL"), help="published site, to reuse briefings")
    ap.add_argument("--max-calls", type=int, default=int(os.environ.get("BRIEFING_MAX_CALLS", 8)))
    ap.add_argument("--offline", action="store_true", help="no model calls; template briefings only")
    a = ap.parse_args()
    doc = build(a.base_url, a.max_calls, a.offline)
    print(f"Briefings: {doc['counts']}")
    print(f"Saved {OUT.relative_to(DATA_DIR)}")


if __name__ == "__main__":
    main()
