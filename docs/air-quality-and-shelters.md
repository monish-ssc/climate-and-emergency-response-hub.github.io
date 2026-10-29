# Air quality (smoke) and reception centres

## Air quality: `data/air_quality_pipeline.py`

| Mode | Command (Colab, repo root) | Output |
|---|---|---|
| Live | `!python data/air_quality_pipeline.py` | `data/pipeline/air_quality/<regionId>_aqhi.json`, `data/pipeline/aqhi_stations_latest.geojson` |
| Replay | `!python data/air_quality_pipeline.py --replay` | `data/scenarios/<fireId>/air_quality.json` |

- **Live** uses ECCC GeoMet AQHI (`aqhi-stations`, `aqhi-observations-realtime`, `aqhi-forecasts-realtime`). It takes the nearest station within 150 km of each region (check `station.distanceKm`), or `dataMode: "unavailable"` if there isn't one. Fields: `current`, `forecast[]`, `forecastPeak`, `level` (low / moderate / high / very high), `healthMessage`.
- **Replay** uses BC Ministry of Environment hourly PM2.5, converted to **AQHI+** = ceil(PM2.5 / 10), the index BC uses during wildfire smoke. `steps[]` uses the same `index` and `t` as `timeline.json`, so step *i* of the fire timeline lines up with step *i* of the smoke data. The 2023 replay uses verified data and the 2026 replay uses unverified year-to-date data. The script searches the BC FTP server for the right PM2.5 file and prints what it found. The files are large, so after the first run you can download one and pass `--pm25 2023=/content/PM25.csv`.
- Clinton has no monitoring station of its own, so its replay uses the nearest stations within 150 km (listed in `stations[]` with `distanceKm`). Say so in the UI.

## Reception centres: `data/build_reception_centres.py`

- The source data is `data/resources/reception_centres.csv`, hand-compiled from EmergencyInfoBC, the City of West Kelowna and CFJC/Castanet reporting. Every row has a `source_url`. Add rows freely.
- `!python data/build_reception_centres.py` geocodes rows that have no coordinates (OpenStreetMap Nominatim) and writes the coordinates back to the CSV. Then it writes:
  - `data/resources/reception_centres.geojson`, one map layer for all centres.
  - `data/scenarios/<fireId>/resources.json` with `receptionCentres[]`. Each centre has `opensAtStep`, which is the replay step at which to show it; null means the date isn't known. It also has `distanceFromRegionKm`, `distanceFromFireKm` (at opening) and `nearestHospitals[]` (3, from the healthcare pipeline).
- Capacity was never published, so don't show it.

## Ideas for the AI briefing
- "Smoke reached AQHI+ 10+ at West Kelowna on <t>, N hours before the score hit critical": use `summary.peak` and `summary.firstHighOrWorse`.
- "Clinton evacuees were sent to Kamloops, about 95 km away. Nearest hospital: Royal Inland, about 3 km from the centre."
