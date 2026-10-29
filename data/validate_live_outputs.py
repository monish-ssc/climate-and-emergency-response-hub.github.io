"""Fail when a live-data refresh is incomplete, stale, or contract-invalid."""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import referencing


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONTRACTS_DIR = ROOT / "api" / "contracts"
LAYER_FILES = (
    "active_fires.geojson",
    "active_fire_extents.geojson",
    "fire_weather_alerts.geojson",
    "stations.geojson",
    "river_conditions_latest.geojson",
    "active_alerts.geojson",
    "hurricane_tracks_latest.geojson",
    "hurricane_historical_storms.geojson",
    "hurricane_active_alerts.geojson",
)
RISK_PATHS = {
    "wildfire": DATA_DIR / "pipeline" / "risk" / "{region}_wildfire.json",
    "flood": DATA_DIR / "pipeline" / "risk" / "{region}_flood.json",
    "severe_storm": DATA_DIR / "samples" / "{region}_severe_storm.json",
}


def load_json(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"Missing output: {path.relative_to(ROOT)}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in {path.relative_to(ROOT)}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"Expected a JSON object in {path.relative_to(ROOT)}")
    return document


def parse_time(value: object, field: str, path: Path) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{path.relative_to(ROOT)}: missing {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"{path.relative_to(ROOT)}: invalid {field} timestamp {value!r}"
        ) from error
    if parsed.tzinfo is None:
        raise ValueError(f"{path.relative_to(ROOT)}: {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def risk_validator() -> jsonschema.Draft202012Validator:
    schemas = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in CONTRACTS_DIR.glob("*.schema.json")
    ]
    registry = referencing.Registry().with_resources(
        (schema["$id"], referencing.Resource.from_contents(schema))
        for schema in schemas
    )
    schema = next(
        schema
        for schema in schemas
        if schema["$id"].endswith("/risk-response.schema.json")
    )
    return jsonschema.Draft202012Validator(schema, registry=registry)


def validate_layers() -> None:
    for filename in LAYER_FILES:
        path = DATA_DIR / "pipeline" / filename
        document = load_json(path)
        if document.get("type") != "FeatureCollection" or not isinstance(
            document.get("features"), list
        ):
            raise ValueError(
                f"{path.relative_to(ROOT)}: expected a GeoJSON FeatureCollection"
            )
        print(f"layer ok: {filename} ({len(document['features'])} features)")


def validate_risks(
    now: datetime, max_age: timedelta
) -> dict[tuple[str, str], tuple[str, str]]:
    regions = load_json(DATA_DIR / "scenarios.json").get("regions")
    if not isinstance(regions, list) or not regions:
        raise ValueError("data/scenarios.json: expected at least one region")
    validator = risk_validator()
    expected: dict[tuple[str, str], tuple[str, str]] = {}
    for region in regions:
        region_id = region.get("id")
        if not isinstance(region_id, str) or not region_id:
            raise ValueError("data/scenarios.json: every region requires an id")
        for hazard, pattern in RISK_PATHS.items():
            path = Path(str(pattern).format(region=region_id))
            document = load_json(path)
            errors = sorted(
                validator.iter_errors(document),
                key=lambda error: tuple(map(str, error.path)),
            )
            if errors:
                raise ValueError(
                    f"{path.relative_to(ROOT)}: risk contract failed: {errors[0].message}"
                )
            if document["region"]["id"] != region_id or document["hazard"] != hazard:
                raise ValueError(
                    f"{path.relative_to(ROOT)}: region or hazard does not match its filename"
                )
            generated = parse_time(document.get("generatedAt"), "generatedAt", path)
            expires = parse_time(document.get("expiresAt"), "expiresAt", path)
            if generated > now + timedelta(minutes=5):
                raise ValueError(f"{path.relative_to(ROOT)}: generatedAt is in the future")
            if now - generated > max_age:
                raise ValueError(
                    f"{path.relative_to(ROOT)}: generatedAt is older than {max_age}"
                )
            if expires <= now:
                raise ValueError(f"{path.relative_to(ROOT)}: snapshot is already expired")
            expected[(region_id, hazard)] = (
                path.relative_to(DATA_DIR).as_posix(),
                document["generatedAt"],
            )
            print(f"risk ok: {region_id}/{hazard} generated {document['generatedAt']}")
    return expected


def validate_index(
    now: datetime,
    max_age: timedelta,
    expected: dict[tuple[str, str], tuple[str, str]],
) -> None:
    path = DATA_DIR / "pipeline" / "risk" / "index.json"
    document = load_json(path)
    built = parse_time(document.get("builtAt"), "builtAt", path)
    if built > now + timedelta(minutes=5) or now - built > max_age:
        raise ValueError(f"{path.relative_to(ROOT)}: builtAt is outside the refresh window")
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"{path.relative_to(ROOT)}: entries must be an array")
    by_key = {
        (entry.get("regionId"), entry.get("hazard")): entry for entry in entries
    }
    missing = sorted(set(expected) - set(by_key))
    if missing:
        raise ValueError(f"{path.relative_to(ROOT)}: missing risk entries: {missing}")
    for key, (expected_path, expected_generated) in expected.items():
        entry = by_key[key]
        if entry.get("path") != expected_path:
            raise ValueError(
                f"{path.relative_to(ROOT)}: {key} points to {entry.get('path')!r}, "
                f"expected {expected_path!r}"
            )
        if entry.get("generatedAt") != expected_generated:
            raise ValueError(
                f"{path.relative_to(ROOT)}: {key} does not reference the refreshed snapshot"
            )
    print(f"index ok: {len(entries)} entries built {document['builtAt']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-age-minutes",
        type=int,
        default=120,
        help="Maximum accepted age for newly generated snapshots (default: 120)",
    )
    args = parser.parse_args()
    if args.max_age_minutes <= 0:
        parser.error("--max-age-minutes must be positive")
    now = datetime.now(timezone.utc)
    max_age = timedelta(minutes=args.max_age_minutes)
    validate_layers()
    expected = validate_risks(now, max_age)
    validate_index(now, max_age, expected)
    print("Live-data refresh validation passed.")


if __name__ == "__main__":
    main()

