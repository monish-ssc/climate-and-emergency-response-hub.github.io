"""
fire_history.py - find a good wildfire to replay and inspect its full history.

Source: CWFIS "reported fires" layer (NRCan), free, no key. Each record is one
status snapshot of a fire (size, stage of control) valid from record_start to
record_end, so a fire's records together are its growth timeline.

Two steps, set at the top:
  1. FIND_CANDIDATES = True  -> lists the biggest fires in PROVINCE this year,
     with how many history records each has. Pick 2-3 from that list.
  2. FIRE_IDS = [...]        -> downloads each fire's full history and prints a
     timeline report, a daily table and a growth chart. Saves files the replay
     builder can use in data/scenarios/<fire id>/.

Runs as is in Google Colab (paste into one cell). Needs pandas; matplotlib for
the chart (both preinstalled in Colab).
"""

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

# ---------------------------------------------------------------- settings
PROVINCE = "BC"            # agency code: BC, AB, SK, MB, ON, QC, NT, YT, PC (Parks Canada) ...
FIND_CANDIDATES = True     # step 1
MIN_SIZE_HA = 5000         # only list fires that reached at least this size
FIRE_IDS = [               # step 2: national_fire_id or agency_fire_id values
    "2026_BC_2026-C40983",
]
# -------------------------------------------------------------------------

WFS = "https://geoserver.cwfif.nrcan.gc.ca/geoserver/wfs"
LAYER = "public:cwfif_national_reportedfires"
STAGES = {"OC": "Out of control", "BH": "Being held", "UC": "Under control", "EX": "Out"}
pd.set_option("display.width", 200, "display.max_columns", 30)


def get_features(cql, sort_by="record_start A", count=10000):
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeName": LAYER, "outputFormat": "application/json", "srsName": "EPSG:4326",
        "count": str(count), "sortBy": sort_by, "CQL_FILTER": cql,
    }
    url = WFS + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "climate-hub-hackathon/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
    matched = data.get("numberMatched")
    if isinstance(matched, int) and matched > len(data["features"]):
        print(f"  WARNING: {matched} records match but only {len(data['features'])} returned "
              "(10,000 cap). Narrow the filter, e.g. add a date range.")
    return data, url


def to_frame(features):
    rows = []
    for f in features:
        p = dict(f.get("properties") or {})
        g = f.get("geometry") or {}
        if g.get("type") == "Point":
            p["lon"], p["lat"] = g["coordinates"][:2]
        rows.append(p)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for c in ("record_start", "record_end", "status_date", "situation_report_date"):
        if c in df:
            df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
    for c in ("fire_size", "percent_contained", "severity_nearest_dsr", "fire_type_ics"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").where(lambda s: s >= 0)  # -1 = unknown
    return df


def km_between(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_candidates():
    print(f"\nFinding {PROVINCE} fires that reached {MIN_SIZE_HA:,} ha or more ...")
    data, _ = get_features(f"agency_code='{PROVINCE}' AND fire_size>={MIN_SIZE_HA}", sort_by="fire_size D")
    df = to_frame(data["features"])
    if df.empty:
        print("  None found. Lower MIN_SIZE_HA or change PROVINCE.")
        return
    g = df.groupby("national_fire_id")
    out = pd.DataFrame({
        "agency_fire_id": g["agency_fire_id"].first(),
        "max_size_ha": g["fire_size"].max().round(0),
        "records_in_sample": g.size(),
        "first_record": g["record_start"].min().dt.strftime("%Y-%m-%d"),
        "last_record": g["record_start"].max().dt.strftime("%Y-%m-%d"),
        "stages_seen": g["stage_of_control_status"].agg(lambda s: ",".join(sorted(set(s.dropna())))),
        "cause": g["national_fire_cause"].first(),
        "response": g["response_type"].agg(lambda s: s.mode().iat[0] if not s.mode().empty else ""),
    }).sort_values("max_size_ha", ascending=False)
    print(out.head(15).to_string())
    print("\nGood replay candidates: many records, an OC stage early on, a long span, "
          "and near communities (check on a map). 'records_in_sample' only counts records "
          f">= {MIN_SIZE_HA:,} ha; the full history in step 2 will have more.")


def fire_report(fire_id):
    field = "national_fire_id" if fire_id.count("_") >= 2 else "agency_fire_id"
    print(f"\n{'=' * 80}\nFire {fire_id}\n{'=' * 80}")
    data, url = get_features(f"{field}='{fire_id}'")
    df = to_frame(data["features"])
    if df.empty:
        print("  No records. Check the id (copy it from the candidate list).")
        return
    df = df.sort_values("record_start").reset_index(drop=True)

    # Records with no change carry no new information for a replay; keep changes only.
    key = ["fire_size", "stage_of_control_status", "lat", "lon"]
    changed = df[key].astype(str).ne(df[key].astype(str).shift()).any(axis=1)
    steps = df[changed]

    span = df["record_start"].max() - df["record_start"].min()
    gaps_h = df["record_start"].diff().dt.total_seconds().div(3600).dropna()
    size = df["fire_size"]
    shrinks = int((size.diff() < 0).sum())
    lat0, lon0 = df["lat"].iloc[0], df["lon"].iloc[0]
    max_shift = max(km_between(lat0, lon0, a, b) for a, b in zip(df["lat"], df["lon"]))

    print(f"Agency id      : {df['agency_fire_id'].iloc[0]}   agency {df['agency_code'].iloc[0]}")
    print(f"Cause / response: {df['national_fire_cause'].iloc[0]} / {', '.join(df['response_type'].dropna().unique())}")
    print(f"Records        : {len(df)} total, {len(steps)} with a change in size, stage or location")
    print(f"Time span      : {df['record_start'].min():%Y-%m-%d %H:%M} to {df['record_start'].max():%Y-%m-%d %H:%M} UTC ({span.days} days)")
    if not gaps_h.empty:
        print(f"Update interval: median {gaps_h.median():.1f} h, longest gap {gaps_h.max():.0f} h")
    print(f"Size           : first {size.dropna().iloc[0]:,.1f} ha, max {size.max():,.1f} ha, last {size.dropna().iloc[-1]:,.1f} ha"
          if size.notna().any() else "Size           : not reported")
    print(f"Size decreases : {shrinks} (revisions; the replay should smooth these)")
    print(f"Location moves : up to {max_shift:.1f} km from the first point")

    stage = df["stage_of_control_status"]
    transitions = df[stage.ne(stage.shift())][["record_start", "stage_of_control_status", "fire_size"]]
    print("\nStage changes:")
    for _, r in transitions.iterrows():
        print(f"  {r.record_start:%Y-%m-%d %H:%M}  {STAGES.get(r.stage_of_control_status, r.stage_of_control_status):<15} "
              f"{r.fire_size:>12,.1f} ha" if pd.notna(r.fire_size) else f"  {r.record_start:%Y-%m-%d %H:%M}  {r.stage_of_control_status}")

    daily = (df.set_index("record_start")
               .resample("1D")
               .agg({"fire_size": "max", "stage_of_control_status": "last", "lat": "last", "lon": "last"})
               .dropna(subset=["fire_size"]))
    daily["growth_ha"] = daily["fire_size"].diff()
    daily["radius_km"] = (daily["fire_size"] * 10000 / math.pi) ** 0.5 / 1000   # circle with the same area
    print(f"\nDaily table ({len(daily)} days with data), first 10 and last 5:")
    show = daily.round({"fire_size": 1, "growth_ha": 1, "radius_km": 2, "lat": 4, "lon": 4})
    print(pd.concat([show.head(10), show.tail(5)]).to_string() if len(show) > 15 else show.to_string())

    quiet_days = int((daily["growth_ha"].fillna(0) <= 0).sum())
    print(f"\nReplay check: {len(daily)} days, {len(daily) - quiet_days} days of growth, "
          f"{'has' if (stage == 'OC').any() else 'no'} out-of-control period, "
          f"growth x{(size.max() / max(size.dropna().iloc[0], 0.1)):,.0f} from first record.")

    folder = os.path.join("data", "scenarios", fire_id)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "history_raw.geojson"), "w") as fh:
        json.dump({**data, "source_url": url}, fh)
    df.to_csv(os.path.join(folder, "history_records.csv"), index=False)
    daily.to_csv(os.path.join(folder, "history_daily.csv"))
    print(f"Saved: {folder}/history_raw.geojson, history_records.csv, history_daily.csv")

    try:
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(10, 3.5))
        ax.step(df["record_start"], df["fire_size"], where="post", color="#C2410C")
        for _, r in transitions.iterrows():
            ax.axvline(r.record_start, color="#94A3B8", lw=0.8, ls="--")
            ax.annotate(r.stage_of_control_status, (r.record_start, ax.get_ylim()[1]), fontsize=8,
                        va="top", ha="left", color="#334155")
        ax.set_title(f"{fire_id}: reported size over time (dashed = stage change)")
        ax.set_ylabel("hectares")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(folder, "growth.png"), dpi=120)
        plt.show()
    except ImportError:
        pass


if FIND_CANDIDATES:
    find_candidates()
for fid in FIRE_IDS:
    try:
        fire_report(fid)
    except Exception as e:  # noqa: BLE001
        print(f"\n{fid}: FAILED - {e}")
